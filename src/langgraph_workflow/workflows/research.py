"""Read-only research: validate -> scope -> investigate (fan-out, bounded) ->
collect (deterministic fan-in, provenance, conflicts) -> resolve? ->
synthesize -> verify deliverable.

Every job is ``access="read"``; a receipt reporting changed files is a
read-only violation, never a silent upgrade to a writer. Read-only is not
permission-free: the host still authorizes each job (including network use).
"""

from __future__ import annotations

from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from ..contracts import DecisionResponse, PendingDecision, WorkflowRequest, digest
from ..policy import effective_limits
from ..state import (
    PARKING,
    BaseState,
    Runtime,
    bounded,
    check_evidence,
    merge_receipts,
    spec,
    stop_for,
)

DEFINITION_VERSION = "research/1"


class State(BaseState, total=False):
    jobs: Annotated[dict, merge_receipts]
    findings: Annotated[dict, merge_receipts]
    investigations: list
    conflicts: list
    unsourced: list
    resolution: dict | None
    synthesis: dict | None


def required_capabilities(request: WorkflowRequest) -> set[str]:
    needed = {"jobs.read", "events", "cancel"}
    if request.checks:
        needed.add("verify")
    return needed


def _normalize(question: str) -> str:
    return " ".join(question.lower().split())


def build(rt: Runtime) -> StateGraph:
    def guard(state: dict, phase: str) -> Command | None:
        if rt.cancelled(state):
            return Command(update={"status": "cancel_requested", "blocker": "cancelled",
                                   "phase": phase}, goto="finish")
        rt.emit(state, "workflow.phase", key=phase, payload={"phase": phase}, diagnostic=True)
        return None

    def validate(state: State) -> Command:
        if stopped := guard(state, "validate"):
            return stopped
        request = WorkflowRequest.from_dict(state["request"])
        missing = required_capabilities(request) - rt.host.capabilities()
        admission = rt.host.admit(request) if not missing else None
        if admission is not None and admission.conflict_approval and "decisions" not in (
            rt.host.capabilities()
        ):
            missing = {"decisions"}
        if missing:
            return Command(update={"phase": "validate", "status": "waiting_for_capability",
                                   "blocker": "missing_capability",
                                   "detail": ",".join(sorted(missing))}, goto="validate")
        if not admission.admitted:
            status = "budget_exhausted" if admission.reason == "budget_exhausted" else "blocked"
            return Command(update={"phase": "validate", "status": status,
                                   "blocker": admission.reason or "not_admitted"},
                           goto="validate" if status == "budget_exhausted" else "finish")
        rt.emit(state, "workflow.started", key="started",
                payload={"workflow": request.workflow, "checks": len(request.checks)})
        return Command(update={"admission": admission.to_dict(), "phase": "validate",
                               "limits": effective_limits(admission.limits, request.limits),
                               "status": "running", "blocker": "", "detail": ""},
                       goto="scope")

    def scope(state: State) -> Command:
        if stopped := guard(state, "scope"):
            return stopped
        request, limits = state["request"], state["limits"]
        questions = list(request["investigations"]) or [request["goal"]]
        seen: dict[str, int] = {}
        planned, duplicates = [], []
        for question in questions:
            key = digest(_normalize(question))
            if key in seen:
                duplicates.append(question[:200])
                continue
            seen[key] = len(planned)
            planned.append({"index": len(planned), "question": question,
                            "key": f"investigate-{len(planned)}-{key[:10]}"})
        if len(planned) > limits["max_investigations"]:
            return Command(update={"phase": "scope", "status": "blocked",
                                   "blocker": "scope_exceeds_limit",
                                   "detail": f"{len(planned)} > {limits['max_investigations']}"},
                           goto="finish")
        # Reserve the whole fan-out plus synthesis before any branch starts.
        pending_jobs = sum(1 for p in planned
                           if f"{request['attempt_id']}/{p['key']}" not in state.get("jobs", {}))
        if state.get("job_count", 0) + pending_jobs + 1 > limits["max_jobs"]:
            return Command(update={"phase": "scope", "status": "budget_exhausted",
                                   "blocker": "budget_exhausted"}, goto="scope")
        routing = {
            "mode": "parallel" if len(planned) > 1 else "single",
            "reason": ("independent sub-questions supplied by the request"
                       if len(planned) > 1 else "one question"),
            "investigations": len(planned),
            "max_parallel": limits["max_parallel_reads"],
            "duplicates_dropped": duplicates,
        }
        rt.emit(state, "workflow.routing", key="routing", payload=routing)
        return Command(update={"phase": "scope", "investigations": planned, "routing": routing},
                       goto=[_send(state, item) for item in planned])

    def _send(state: dict, item: dict) -> Send:
        return Send("investigate", {
            "request": state["request"], "limits": state["limits"],
            "jobs": state.get("jobs", {}), "job_count": state.get("job_count", 0),
            "item": item,
        })

    def investigate(payload: dict) -> dict:
        item = payload["item"]
        if rt.cancelled(payload):
            return {}  # parallel branches never write shared scalars; collect stops
        receipt, update = rt.run_job(payload, spec(
            payload, "investigate", "read", item["key"], item["question"],
            {"index": item["index"]}, parent=f"{payload['request']['attempt_id']}/scope"))
        output = receipt.output
        finding = {
            "index": item["index"], "question": item["question"][:500],
            "operation_id": receipt.operation_id, "job_id": receipt.job_id,
            "status": receipt.status, "detail": receipt.detail,
            "claims": output.get("claims", {}) if isinstance(output.get("claims"), dict) else {},
            "sources": [s for s in output.get("sources", []) if isinstance(s, str)][:32],
        }
        return {**update, "findings": {receipt.operation_id: finding}}

    def collect(state: State) -> Command:
        if stopped := guard(state, "collect"):
            return stopped
        findings = sorted(state.get("findings", {}).values(), key=lambda f: f["index"])
        for finding in findings:
            if finding["status"] == "settled":
                continue
            stopped = stop_for(finding["status"], finding["detail"],
                               finding["operation_id"]) or {
                "status": "needs_review", "blocker": "investigation_failed",
                "detail": finding["operation_id"]}
            if stopped["status"] in PARKING:
                # Re-enter only the unsettled branches on resume.
                redo = [i for i in state["investigations"]
                        if f"{state['request']['attempt_id']}/{i['key']}" in {
                            f["operation_id"] for f in findings if f["status"] != "settled"}]
                return Command(update={"phase": "collect", **stopped},
                               goto=[_send(state, i) for i in redo])
            return Command(update={"phase": "collect", **stopped}, goto="finish")
        # Deterministic aggregation: index order, sorted claim keys.
        claims: dict[str, dict[int, Any]] = {}
        unsourced = []
        for finding in findings:
            if not finding["sources"]:
                unsourced.append(finding["index"])
            for key in sorted(finding["claims"]):
                claims.setdefault(key, {})[finding["index"]] = finding["claims"][key]
        conflicts = [
            {"key": key, "values": {str(i): v for i, v in sorted(values.items())}}
            for key, values in sorted(claims.items())
            if len({digest(v) for v in values.values()}) > 1
        ]
        update = {"phase": "collect", "conflicts": conflicts, "unsourced": unsourced,
                  "status": "running"}
        if conflicts and state["admission"]["conflict_approval"]:
            return Command(update=update, goto="resolve")
        return Command(update=update, goto="synthesize")

    def resolve(state: State) -> Command:
        if stopped := guard(state, "resolve"):
            return stopped
        request, conflicts = state["request"], state["conflicts"]
        pending = PendingDecision(
            decision_id=f"{request['attempt_id']}:conflicts:1",
            run_id=request["run_id"], attempt_id=request["attempt_id"],
            kind="conflict_resolution", revision=1, digest=digest(conflicts),
            summary=f"{len(conflicts)} conflicting claim(s): "
                    + ", ".join(c["key"] for c in conflicts)[:1500],
            options=("report", *(f"prefer:{i['index']}" for i in state["investigations"])),
        )
        rt.emit(state, "decision.pending", key=pending.decision_id,
                payload={"decision_id": pending.decision_id, "kind": pending.kind,
                         "summary": pending.summary, "options": list(pending.options)})
        response = DecisionResponse.from_dict(interrupt(pending.to_dict()))
        if (response.decision_id, response.digest) != (pending.decision_id, pending.digest):
            return Command(update={"status": "blocked", "blocker": "decision_mismatch"},
                           goto="finish")
        rt.emit(state, "decision.resolved", key=pending.decision_id,
                payload={"decision_id": pending.decision_id, "choice": response.choice})
        return Command(update={"phase": "resolve", "resolution": {"choice": response.choice}},
                       goto="synthesize")

    def synthesize(state: State) -> Command:
        if stopped := guard(state, "synthesize"):
            return stopped
        findings = sorted(state["findings"].values(), key=lambda f: f["index"])
        receipt, update = rt.run_job(state, spec(
            state, "synthesize", "read", "synthesize", state["request"]["goal"],
            {"findings": [{"index": f["index"], "job_id": f["job_id"], "claims": f["claims"],
                           "sources": f["sources"]} for f in findings],
             "conflicts": state["conflicts"], "resolution": state.get("resolution")}))
        update["phase"] = "synthesize"
        if receipt.status == "failed":
            return Command(update={**update, "status": "needs_review",
                                   "blocker": "synthesis_failed"}, goto="finish")
        if receipt.status != "settled":
            stopped = stop_for(receipt.status, receipt.detail, receipt.operation_id)
            return Command(update={**update, **stopped},
                           goto="synthesize" if stopped["status"] in PARKING else "finish")
        return Command(update={**update, "status": "running",
                               "synthesis": {"job_id": receipt.job_id, **receipt.output,
                                             "artifacts": list(receipt.artifacts)}},
                       goto="verify")

    def verify(state: State) -> Command:
        if stopped := guard(state, "verify"):
            return stopped
        synthesis, findings = state["synthesis"], state["findings"].values()
        problems = []
        # Provenance: each synthesized claim must match a sourced finding.
        supported = {(k, digest(v)) for f in findings if f["sources"]
                     for k, v in f["claims"].items()}
        claims = synthesis.get("claims", {}) if isinstance(synthesis.get("claims"), dict) else {}
        if any((k, digest(v)) not in supported for k, v in claims.items()):
            problems.append("unsupported_claim")
        choice = (state.get("resolution") or {}).get("choice", "report")
        if state["conflicts"] and choice == "report":
            reported = set(synthesis.get("conflicts_reported", []))
            if not {c["key"] for c in state["conflicts"]} <= reported:
                problems.append("conflicts_unreported")
        request = WorkflowRequest.from_dict(state["request"])
        report = (rt.host.verify(request, request.checks, final=True).to_dict()
                  if request.checks else {"status": "needs_review", "results": []})
        outcome, blocker = check_evidence(request.checks, report)
        if problems:
            outcome, blocker = "needs_review", problems[0]
        rt.emit(state, "verification.result", key="final",
                payload={"outcome": outcome, "blocker": blocker, "problems": problems,
                         "checks": [[r["check_id"], r["state"]] for r in report["results"]]})
        status = "verified" if outcome == "passed" else "needs_review"
        return Command(update={"phase": "verify", "status": status,
                               "blocker": "" if status == "verified" else blocker,
                               "verification": {**report, "outcome": outcome,
                                                "problems": problems, "final": True}},
                       goto="finish")

    def finish(state: State) -> dict:
        return rt.finish(state)

    graph = StateGraph(State)
    for name, fn in (("validate", validate), ("scope", scope), ("collect", collect),
                     ("resolve", resolve), ("synthesize", synthesize), ("verify", verify)):
        graph.add_node(name, bounded(fn))
    graph.add_node("investigate", investigate)  # Send target: bounded by max_investigations
    graph.add_node("finish", finish)
    graph.add_edge(START, "validate")
    graph.add_edge("investigate", "collect")
    graph.add_edge("finish", END)
    return graph
