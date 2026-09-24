"""Reference Locus adapter: ``WorkflowHost`` over real Locus services.

Integration-harness code. It imports ``ollama_code`` (Locus) and
``langgraph_workflow``; the package never imports Locus. In Prompt 2 this
moves into a Locus feature module and is wired through request-owned
dependencies, not ``server.py`` globals.

Verified against Locus 5ac5b5b1 + patches/0001 (``TeamOrchestrator.run_read_job``).
Without that patch the adapter omits ``jobs.read`` and workflows that need
read jobs wait for the capability instead of calling a private method.

Mapping (see docs/locus-integration-map.md):
- read jobs    -> TeamOrchestrator.run_read_job (scheduler lease, usage ledger,
                  agent_job_* events, stop handling)
- write jobs   -> AgentCore.run_turn (Locus tool loop, PermissionManager +
                  decider, per-invocation receipts in the task journal)
- verification -> TaskVerifier / TaskStateStore receipts
- job identity -> RunStore job_attempts ("{run}:{job}:{n}"), job id encodes
                  the operation id and full input fingerprint
- events       -> RunStore.append_event("workflow_event"); event_id is the
                  primary key, so redelivery is a no-op; seq is the cursor
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ollama_code.capsule_progress import workspace_state
from ollama_code.orchestration import (
    AgentJob,
    AgentProfile,
    OrchestrationBudget,
    OrchestrationError,
    TeamOrchestrator,
)
from ollama_code.task_journal import TaskJournal
from ollama_code.task_state import TaskStateError, TaskStateStore, TaskVerifier
from ollama_code.usage_ledger import UsageLimitError

from langgraph_workflow import (
    Admission,
    CheckResult,
    DecisionResponse,
    JobReceipt,
    JobSpec,
    VerificationReport,
    WorkflowEvent,
    WorkflowRequest,
)
from langgraph_workflow.contracts import digest

ADAPTER_VERSION = "locus-reference/1"
ROLE = {"inspect": "researcher", "plan": "planner", "review": "reviewer",
        "investigate": "researcher", "synthesize": "researcher"}
RESPONSE_CONTRACT = {
    "inspect": '{"summary": str}',
    "plan": '{"plan": {"steps": [{"title": str}]}}',
    "review": '{"verdict": "approve"|"changes_requested", "findings": [str]}',
    "investigate": '{"claims": {str: value}, "sources": [str]}',
    "synthesize": '{"summary": str, "claims": {}, "sources": [str], "conflicts_reported": [str]}',
    "write": "Make the change with your tools, then summarize it.",
}
PERSISTED = ("agent_job_started", "agent_job_completed", "agent_job_incomplete",
             "agent_job_continuing", "scheduler_lease_waiting", "scheduler_lease_acquired",
             "scheduler_lease_released")

Decider = Callable[[str, str, str, str], str]


def _json_object(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class LocusHost:
    """One adapter instance serves one Locus run/task/workspace."""

    def __init__(self, *, core: Any, runs: Any, run_id: str, session_id: str, task_id: str,
                 workspace: str, reader: AgentProfile, budget: OrchestrationBudget,
                 decider: Decider, actor: str, plan_approval: bool = False,
                 conflict_approval: bool = False, writer_call_limit: int = 12) -> None:
        self.core, self.runs = core, runs
        self.run_id, self.session_id, self.task_id = run_id, session_id, task_id
        self.workspace = str(Path(workspace).resolve())
        self.checkout_ref = "path:" + self.workspace
        self.reader, self.budget, self.decider, self.actor = reader, budget, decider, actor
        self.plan_approval, self.conflict_approval = plan_approval, conflict_approval
        self.writer_call_limit = writer_call_limit
        self.store = TaskStateStore(runs)
        self.stream: deque = deque(maxlen=512)  # transient token bridge; bounded
        self.core_events: deque = deque(maxlen=512)
        self.permission_decisions: list[tuple[str, str]] = []
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        core.on_event(lambda event: self.core_events.append(event.get("type")))
        self.orchestrator = TeamOrchestrator(self._emit, self._should_stop, run_store=runs)
        # Resume never restores consumed allowance: seed the orchestrator's
        # call counter from Locus's durable job records for this run.
        calls = sum(int((a.get("result") or {}).get("model_calls") or 0)
                    for a in (runs.attempts(run_id) if runs.run(run_id) else []))
        self.orchestrator.restore_usage({"model_calls": calls}, budget)

    # -- admission ---------------------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        caps = {"jobs.write", "verify", "decisions", "events", "cancel"}
        if hasattr(self.orchestrator, "run_read_job"):
            caps.add("jobs.read")
        return frozenset(caps)

    def policy_ref(self) -> str:
        route = {k: self.reader.route.get(k) for k in ("provider", "host", "account_id",
                                                       "base_url") if k in self.reader.route}
        return digest({"adapter": ADAPTER_VERSION, "permissions": self.core.perms.state(),
                       "reader": [self.reader.id, self.reader.model, route],
                       "writer_model": self.core.model, "workspace": self.workspace})

    def _admission(self) -> Admission:
        return Admission(True, policy_ref=self.policy_ref(), plan_approval=self.plan_approval,
                         conflict_approval=self.conflict_approval,
                         limits={"max_parallel_reads": min(2, self.budget.max_concurrent_calls)})

    def admit(self, request: WorkflowRequest) -> Admission:
        if request.checkout_ref and request.checkout_ref != self.checkout_ref:
            return Admission(False, reason="checkout_mismatch")
        if self.runs.run(self.run_id) is None:
            self.runs.start_run(self.run_id, session_id=self.session_id,
                                workspace_root=self.workspace, execution_path=self.workspace,
                                request=request.goal, state="running")
        if self.store.get(self.task_id) is None:
            self.store.ensure(self.task_id, request=request.goal, revision=1,
                              workspace=self.workspace, execution=self.workspace)
        self.core.task_journal = TaskJournal.bind(self.runs, self.runs.run(self.run_id))
        return self._admission()

    def revalidate(self, request: WorkflowRequest, policy_ref: str) -> Admission:
        if policy_ref != self.policy_ref():
            return Admission(False, reason="policy_revoked")
        if not Path(self.workspace).is_dir() or (
                request.checkout_ref and request.checkout_ref != self.checkout_ref):
            return Admission(False, reason="checkout_changed")
        self.core.task_journal = TaskJournal.bind(self.runs, self.runs.run(self.run_id))
        return self._admission()

    def authorize_decision(self, response: DecisionResponse) -> bool:
        # The Locus route layer authenticates the controller; the adapter only
        # accepts decisions attributed to that authenticated actor.
        return response.actor == self.actor

    # -- jobs --------------------------------------------------------------------
    def _should_stop(self) -> bool:
        return self._stop.is_set() or self.core._interrupt.is_set()

    def _emit(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "agent_job_stream":
            self.stream.append((event.get("job_id"), str(event.get("text", ""))[:2000]))
            return
        if kind == "agent_job_completed" and "#" in str(event.get("job_id", "")):
            # Enrich Locus's own completion record so the durable job attempt
            # carries the parsed result; no second completion event is needed.
            result = event.setdefault("result", {})
            result.setdefault("structured", _json_object(str(result.get("output", ""))))
            result.setdefault("workflow_status", "cancelled" if event.get("state") == "stopped"
                              else "failed" if result.get("error") else "settled")
        if kind in PERSISTED:
            self.runs.append_event(self.run_id, event)

    def _decide(self, tool: str, summary: str, detail: str, request_id: str) -> str:
        answer = self.decider(tool, summary, detail, request_id)
        self.permission_decisions.append((tool, answer))
        return answer

    def lookup(self, operation_id: str) -> JobReceipt | None:
        for attempt in reversed(self.runs.attempts(self.run_id)):
            op, _, fingerprint = attempt["job_id"].partition("#")
            if op == operation_id:
                return self._receipt(op, fingerprint, attempt)
        return None

    def _receipt(self, op: str, fingerprint: str, attempt: dict) -> JobReceipt:
        result = attempt.get("result") or {}
        state = attempt["state"]
        if state == "running":
            status = "running" if op in self._inflight else "uncertain"
            return JobReceipt(op, attempt["attempt_id"], status, fingerprint,
                              detail="" if status == "running"
                              else "started_without_observed_outcome")
        status = result.get("workflow_status") or {"completed": "settled",
                                                   "stopped": "cancelled"}.get(state, "failed")
        output = result.get("structured") or {}
        usage = {"calls": int(result.get("model_calls") or 0),
                 "prompt_tokens": int(result.get("prompt_tokens") or 0),
                 "completion_tokens": int(result.get("completion_tokens") or 0),
                 "ledger": "locus task_usage_ledger (authoritative)"}
        usage["coverage"] = ("known" if usage["prompt_tokens"] or usage["completion_tokens"]
                             else "unknown")
        return JobReceipt(op, attempt["attempt_id"], status, fingerprint,
                          output={**output, "summary": str(result.get("output", ""))[:2000]},
                          changed_files=tuple(result.get("changed_files") or ()),
                          evidence=tuple(str(e)[:500] for e in (result.get("evidence") or ())[:32]),
                          usage=usage, detail=str(result.get("error") or "")[:500])

    def execute(self, spec: JobSpec) -> JobReceipt:
        existing = self.lookup(spec.operation_id)
        if existing is not None and existing.status not in ("budget_exhausted", "busy"):
            return existing  # never start an operation twice
        if self._should_stop():
            return JobReceipt(spec.operation_id, "", "cancelled", spec.input_fingerprint,
                              detail="cancelled before start")
        job_id = f"{spec.operation_id}#{spec.input_fingerprint}"
        with self._lock:
            self._inflight.add(spec.operation_id)
        try:
            if spec.access == "read":
                self._read(spec, job_id)
            else:
                self._write(spec, job_id)
        finally:
            with self._lock:
                self._inflight.discard(spec.operation_id)
        return self.lookup(spec.operation_id)

    def _prompt(self, spec: JobSpec) -> str:
        return (f"[{spec.kind}] {spec.instruction}\n\nInputs (JSON): "
                f"{json.dumps(spec.inputs, sort_keys=True)[:12000]}\n\n"
                f"Respond with: {RESPONSE_CONTRACT[spec.kind]}")

    def _close(self, job_id: str, state: str, result: dict) -> None:
        self._emit({"type": "agent_job_completed", "run_id": self.run_id, "job_id": job_id,
                    "node_id": job_id, "state": state, "result": {"job_id": job_id, **result}})

    def _read(self, spec: JobSpec, job_id: str) -> None:
        job = AgentJob(id=job_id, agent_id=self.reader.id, goal=self._prompt(spec),
                       dependencies=(), kind="research", required_role=ROLE[spec.kind],
                       node_id=job_id, execution_kind="read")
        try:
            result = self.orchestrator.run_read_job(self.run_id, job, self.reader, self.budget)
        except InterruptedError:
            self._close(job_id, "stopped", {"error": "cancelled", "workflow_status": "cancelled"})
            return
        except (OrchestrationError, UsageLimitError) as error:
            budget = "budget" in str(error) or isinstance(error, UsageLimitError)
            # Budget refusals happen before the provider call is dispatched.
            self._close(job_id, "failed", {"error": str(error)[:500],
                                           "workflow_status": "budget_exhausted" if budget
                                           else "failed"})
            return
        del result  # run_read_job recorded the (enriched) completion durably

    def _write(self, spec: JobSpec, job_id: str) -> None:
        before = workspace_state(self.workspace)
        self._emit({"type": "agent_job_started", "run_id": self.run_id, "job_id": job_id,
                    "node_id": job_id, "agent_id": "workflow-writer",
                    "agent_name": "Workflow writer", "role": "implementer",
                    "provider": str(self.core.provider), "model": self.core.model,
                    "goal": spec.instruction[:2000], "state": "running"})
        denied_before = sum(1 for _, answer in self.permission_decisions if answer == "deny")
        started = time.monotonic()
        self.core.run_turn(self._prompt(spec), self._decide, allow_tools=True,
                           model_call_limit=self.writer_call_limit, persist_user_message=False)
        turn = dict(self.core.last_turn_result or {})
        after = workspace_state(self.workspace)
        changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        denied = sum(1 for _, a in self.permission_decisions if a == "deny") > denied_before
        if self._should_stop() or turn.get("reason") == "interrupted":
            status = "cancelled"
        elif denied and not changed:
            status = "denied"
        elif turn.get("reason") == "complete":
            status = "settled"
        else:
            status = "failed"
        assistant = next((m.get("content", "") for m in reversed(self.core.messages)
                          if m.get("role") == "assistant"), "")
        self._close(job_id, "completed" if status == "settled" else status, {
            "output": str(assistant)[:4000], "changed_files": changed, "workflow_status": status,
            "model_calls": int(turn.get("model_calls") or 0),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": "" if status == "settled" else str(turn.get("reason") or status)})

    def cancel(self, attempt_id: str) -> bool:
        self._stop.set()
        self.core.interrupt()
        deadline = time.monotonic() + 30
        while self._inflight and time.monotonic() < deadline:
            time.sleep(0.02)
        return not self._inflight

    # -- verification ------------------------------------------------------------
    def verify(self, request: WorkflowRequest, checks: tuple, *, final: bool) -> VerificationReport:
        verifier = TaskVerifier(self.store, self.task_id, self.core, self.run_id)
        try:
            value = verifier.verify([dict(c) for c in checks], self._decide)
        except (TaskStateError, ValueError) as error:
            state = "stale" if "changed" in str(error) else "unsupported"
            return VerificationReport("needs_review", tuple(
                CheckResult(c["id"], state, "", str(error)[:500]) for c in checks))
        receipts = {r["id"]: r for r in self.store.receipts(self.task_id)}
        results = []
        for evidence_id in value["evidence_ids"]:
            receipt = receipts[evidence_id]
            state = receipt["state"]
            if state == "needs_review" and str(receipt.get("detail", "")).startswith(
                    "Permission denied"):
                state = "denied"
            results.append(CheckResult(receipt["check_id"], state,
                                       receipt["id"] if state in ("passed", "failed") else "",
                                       str(receipt.get("detail", ""))[:500]))
        return VerificationReport(value["verification_status"] if value["verification_status"]
                                  in ("passed", "failed", "needs_review") else "needs_review",
                                  tuple(results), revision=str(value["revision"]))

    # -- events ------------------------------------------------------------------
    def publish(self, event: WorkflowEvent) -> None:
        try:
            self.runs.append_event(self.run_id, {
                "type": "workflow_event", "event_id": event.event_id,
                "execution_engine": "langgraph_workflow", "workflow_event": event.to_dict()})
        except sqlite3.IntegrityError:
            pass  # already delivered: event_id is the durable dedupe key
