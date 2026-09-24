"""Host-facing executor.

All methods are synchronous and run the graph on the calling thread until it
completes, waits for a decision, parks, or reaches a requested pause. Nothing
runs in the background: execution never outlives the host call that owns it.
From async code use the ``a*`` wrappers (``asyncio.to_thread``); calling the
blocking methods on an event-loop thread raises instead of stalling the loop.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from langsmith.run_helpers import tracing_context

from .checkpoints import AttemptBusy, CheckpointStore
from .contracts import (
    CONTRACT_VERSION,
    STATE_SCHEMA_VERSION,
    TERMINAL_STATUSES,
    AttemptStatus,
    ContractError,
    DecisionResponse,
    WorkflowRequest,
    digest,
)
from .events import WorkflowEvent, make_event
from .policy import effective_limits
from .ports import WorkflowHost
from .state import Runtime
from .workflows import WORKFLOWS

PARKING = ("uncertain", "waiting_for_capability", "budget_exhausted")


class DecisionRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IncompatibleAttempt(RuntimeError):
    """A saved attempt was created by a different graph/schema/contract version."""


def _not_on_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("blocking executor call on an event-loop thread; use the a* wrapper")


class WorkflowExecutor:
    """Construction is inert: no I/O, threads, or database access."""

    def __init__(
        self,
        host: WorkflowHost,
        store_path: str | os.PathLike,
        *,
        cipher: Any = None,
        require_encryption: bool = False,
        diagnostics: bool = False,
        lease_seconds: float = 120.0,
    ) -> None:
        self.host = host
        self.store = CheckpointStore(store_path, cipher=cipher,
                                     require_encryption=require_encryption)
        self.diagnostics = diagnostics
        self.lease_seconds = lease_seconds
        self._graphs: dict[str, Any] = {}

    # -- capability reporting ------------------------------------------------
    def validate(self, request: WorkflowRequest | dict) -> list[str]:
        """Problems that would stop ``request`` before any job runs."""
        try:
            request = _request(request)
        except ContractError as error:
            return [f"invalid_request: {error}"]
        module = WORKFLOWS[request.workflow]
        missing = module.required_capabilities(request) - self.host.capabilities()
        return [f"missing_capability: {name}" for name in sorted(missing)]

    # -- lifecycle -------------------------------------------------------------
    def start(self, request: WorkflowRequest | dict) -> AttemptStatus:
        """Start an admitted attempt. A repeated start resumes; it never forks."""
        _not_on_event_loop()
        request = _request(request)
        module = WORKFLOWS[request.workflow]
        versions = {"definition": module.DEFINITION_VERSION, "schema": STATE_SCHEMA_VERSION,
                    "contract": CONTRACT_VERSION}
        existing = self.store.register(request, versions)
        if existing is not None:
            if existing["request_fingerprint"] != request.fingerprint:
                raise ContractError("attempt_id already used for a different request")
            return self.resume(request.attempt_id)
        initial = {"request": request.to_dict(), "versions": versions, "phase": "admission",
                   "status": "running", "jobs": {}, "job_count": 0,
                   "limits": effective_limits(request.limits)}
        return self._run(request.attempt_id, initial)

    def resume(self, attempt_id: str) -> AttemptStatus:
        """Continue a paused, parked or interrupted attempt from durable state."""
        _not_on_event_loop()
        values, snapshot = self._snapshot(attempt_id)
        current = self.status(attempt_id)
        if current.status in TERMINAL_STATUSES or current.pending_decision:
            return current
        request = WorkflowRequest.from_dict(values["request"])
        if "admission" in values:
            admission = self.host.revalidate(request, values["admission"]["policy_ref"])
            if not admission.admitted:
                self.store.set_status(attempt_id, f"blocked:{admission.reason or 'revoked'}")
                return self.status(attempt_id)
        return self._run(attempt_id, None, clear_pause=True)

    def decide(self, response: DecisionResponse | dict) -> AttemptStatus:
        """Apply one authenticated decision to the attempt's pending interrupt."""
        _not_on_event_loop()
        try:
            response = (response if isinstance(response, DecisionResponse)
                        else DecisionResponse.from_dict(response))
        except ContractError as error:
            raise DecisionRejected("invalid_response") from error
        attempt = self.store.attempt(response.attempt_id)
        if attempt is None:
            raise DecisionRejected("unknown_attempt")
        if attempt["run_id"] != response.run_id:
            raise DecisionRejected("cross_run")
        values, _ = self._snapshot(response.attempt_id)
        pending = self.status(response.attempt_id).pending_decision
        if pending is None:
            consumed = self.store.decision_consumed(response.decision_id)
            raise DecisionRejected("already_consumed" if consumed else "no_pending_decision")
        if pending["decision_id"] != response.decision_id:
            raise DecisionRejected("stale_decision")
        if (pending["revision"], pending["digest"]) != (response.revision, response.digest):
            raise DecisionRejected("stale_revision")
        if response.choice not in pending["options"]:
            raise DecisionRejected("invalid_choice")
        if response.choice == "edit" and response.edited_plan is None:
            raise DecisionRejected("edit_without_plan")
        if self.store.controls(response.attempt_id)["cancel"]:
            raise DecisionRejected("cancelled")
        if not self.host.authorize_decision(response):
            raise DecisionRejected("unauthorized")
        if self.store.lease_holder(response.attempt_id) is not None:
            raise AttemptBusy(f"attempt {response.attempt_id} is running; decide after it stops")
        request = WorkflowRequest.from_dict(values["request"])
        if not self.host.revalidate(request, values["admission"]["policy_ref"]).admitted:
            raise DecisionRejected("revoked")
        if not self.store.consume_decision(response.decision_id, response.attempt_id,
                                           digest(response.to_dict())):
            raise DecisionRejected("duplicate_decision")
        return self._run(response.attempt_id, Command(resume=response.to_dict()))

    def pause(self, attempt_id: str) -> AttemptStatus:
        """Request a cooperative pause at the next persisted super-step."""
        self.store.request(attempt_id, "pause")
        return self.status(attempt_id)

    def cancel(self, attempt_id: str) -> AttemptStatus:
        """Cancel through the host and drain. Callable from any thread/process.

        Reports ``cancelled`` only after the host confirms quiescence;
        otherwise the attempt stays ``cancel_requested`` and cancel can be
        called again.
        """
        _not_on_event_loop()
        self.store.request(attempt_id, "cancel")
        quiescent = self.host.cancel(attempt_id)
        if self.store.lease_holder(attempt_id) is not None:
            return self.status(attempt_id)  # the running driver routes to finish
        values, snapshot = self._snapshot(attempt_id)
        with self.store.lease(attempt_id, self.lease_seconds):
            if values.get("phase") != "finish":
                resume = Command(resume={"cancelled": True}) if snapshot.interrupts else None
                self._drive(attempt_id, resume)
                values, snapshot = self._snapshot(attempt_id)
            if values.get("status") == "cancel_requested" and (quiescent or
                                                               self.host.cancel(attempt_id)):
                self._graph(values["request"]["workflow"]).update_state(
                    self._config(values), {"status": "cancelled"}, as_node="finish")
                self._publish(make_event("workflow.outcome", values["request"], key="cancelled",
                                         payload={"status": "cancelled"}))
        return self.status(attempt_id)

    def status(self, attempt_id: str) -> AttemptStatus:
        values, snapshot = self._snapshot(attempt_id)
        attempt = self.store.attempt(attempt_id) or {}
        request = values.get("request", {})
        status = values.get("status", "running")
        blocker = values.get("blocker", "")
        pending = None
        # ``snapshot.next`` is not a completion signal: after a crash between a
        # task's saved writes and the next checkpoint it can be empty while
        # work remains. Only the finish node ends a workflow.
        finished = values.get("phase") == "finish"
        if snapshot.interrupts:
            pending = snapshot.interrupts[0].value
            status = "waiting_for_input"
        elif not finished and status not in PARKING:
            holder = self.store.lease_holder(attempt_id)
            if self.store.controls(attempt_id)["cancel"]:
                status = "cancel_requested"
            elif holder is None:
                status, blocker = "paused", blocker or (
                    "paused" if self.store.controls(attempt_id)["pause"] else "interrupted")
            else:
                status = "running"
        saved = attempt.get("status", "")
        if saved.startswith("blocked:") and status not in TERMINAL_STATUSES:
            status, blocker = "blocked", saved.split(":", 1)[1]
        jobs = values.get("jobs", {})
        usage = {"calls": sum(int(j.get("usage", {}).get("calls", 0) or 0) for j in jobs.values()),
                 "coverage": "unknown" if any(j.get("usage", {}).get("coverage") != "known"
                                              for j in jobs.values()) else "known",
                 "authoritative": False}
        return AttemptStatus(
            attempt_id=attempt_id, run_id=request.get("run_id", attempt.get("run_id", "")),
            workflow=request.get("workflow", attempt.get("workflow", "")), status=status,
            phase=values.get("phase", ""), blocker=blocker, detail=values.get("detail", ""),
            pending_decision=pending, jobs={k: v["status"] for k, v in jobs.items()},
            verification=values.get("verification"), repair_rounds=values.get("repair_rounds", 0),
            result=values.get("result", {}), usage=usage,
        )

    def close(self) -> None:
        self.store.close()

    # -- async bridge ------------------------------------------------------------
    async def astart(self, request: WorkflowRequest | dict) -> AttemptStatus:
        return await asyncio.to_thread(self.start, request)

    async def aresume(self, attempt_id: str) -> AttemptStatus:
        return await asyncio.to_thread(self.resume, attempt_id)

    async def adecide(self, response: DecisionResponse | dict) -> AttemptStatus:
        return await asyncio.to_thread(self.decide, response)

    async def acancel(self, attempt_id: str) -> AttemptStatus:
        return await asyncio.to_thread(self.cancel, attempt_id)

    # -- internals ---------------------------------------------------------------
    def _graph(self, workflow: str):
        if workflow not in self._graphs:
            runtime = Runtime(self.host, self.store.controls)
            self._graphs[workflow] = WORKFLOWS[workflow].build(runtime).compile(
                checkpointer=self.store.saver)
        return self._graphs[workflow]

    def _config(self, values_or_request: dict) -> dict:
        request = values_or_request.get("request", values_or_request)
        limits = values_or_request.get("limits") or effective_limits(request.get("limits"))
        # Only the opaque attempt id enters `configurable`, because LangGraph
        # copies configurable values into plaintext checkpoint metadata.
        return {"configurable": {"thread_id": request["attempt_id"]},
                "recursion_limit": limits["max_transitions"],
                "max_concurrency": limits["max_parallel_reads"]}

    def _snapshot(self, attempt_id: str):
        attempt = self.store.attempt(attempt_id)
        if attempt is None:
            raise KeyError(f"unknown attempt {attempt_id}")
        module = WORKFLOWS.get(attempt["workflow"])
        if module is None or (attempt["definition_version"], attempt["schema_version"],
                              attempt["contract_version"]) != (
                module.DEFINITION_VERSION, STATE_SCHEMA_VERSION, CONTRACT_VERSION):
            raise IncompatibleAttempt(
                f"attempt {attempt_id} was saved by {attempt['definition_version']} "
                f"(schema {attempt['schema_version']}, contract {attempt['contract_version']}); "
                "it cannot run under this package version. Inspect it with the version that "
                "created it or start a new attempt.")
        graph = self._graph(attempt["workflow"])
        snapshot = graph.get_state({"configurable": {"thread_id": attempt_id}})
        return snapshot.values or {}, snapshot

    def _run(self, attempt_id: str, graph_input: Any, clear_pause: bool = False) -> AttemptStatus:
        with self.store.lease(attempt_id, self.lease_seconds):
            if clear_pause:  # only the owner may clear another caller's pause
                self.store.request(attempt_id, "pause", False)
            self.store.set_status(attempt_id, "running")
            self._drive(attempt_id, graph_input)
        status = self.status(attempt_id)  # after release, so "paused" is visible
        self.store.set_status(attempt_id, status.status)
        return status

    def _drive(self, attempt_id: str, graph_input: Any) -> None:
        values, snapshot = self._snapshot(attempt_id)
        # Resuming re-emits the current checkpoint first; only later ones
        # reflect work done by this call.
        starting = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
        seed = values or graph_input
        graph = self._graph(seed["request"]["workflow"])
        config = self._config(seed)
        stream = graph.stream(graph_input, config, stream_mode=["custom", "checkpoints"],
                              durability="sync")
        try:
            # Context-local: suppresses LangSmith tracing even when ambient
            # environment variables would enable it, without touching them.
            with tracing_context(enabled=False):
                for mode, chunk in stream:
                    if mode == "custom":
                        self._publish(chunk)
                        continue
                    self.store.renew(attempt_id, self.lease_seconds)
                    checkpoint_id = chunk["config"]["configurable"]["checkpoint_id"]
                    if checkpoint_id == starting or chunk["metadata"].get("source") == "input":
                        continue
                    status = (chunk.get("values") or {}).get("status")
                    if status in PARKING and chunk.get("next"):
                        break  # resumable stop: the same node re-checks on resume
                    if self.store.controls(attempt_id)["pause"] and chunk.get("next"):
                        values = chunk.get("values") or {}
                        self._publish(make_event("workflow.paused", values["request"],
                                                 key=checkpoint_id,
                                                 payload={"phase": values.get("phase", "")}))
                        break
        except GraphRecursionError:
            graph.update_state(config, {"status": "failed", "blocker": "transition_limit"},
                               as_node="finish")
        finally:
            stream.close()

    def _publish(self, event: WorkflowEvent) -> None:
        if event.diagnostic and not self.diagnostics:
            return
        self.host.publish(event)


def _request(value: WorkflowRequest | dict) -> WorkflowRequest:
    return value if isinstance(value, WorkflowRequest) else WorkflowRequest.from_dict(value)
