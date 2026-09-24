"""Verified change: validate -> inspect -> plan -> approve -> implement ->
verify -> (repair -> verify)* -> (review -> repair?)* -> final verify.

Only host verification can produce ``verified``. A supplied plan skips the
inspect/plan jobs; a configured reviewer is never skipped.
"""

from __future__ import annotations

from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..contracts import (
    ContractError,
    DecisionResponse,
    PendingDecision,
    WorkflowRequest,
    digest,
    validate_plan,
)
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

DEFINITION_VERSION = "verified_change/1"


class State(BaseState, total=False):
    jobs: Annotated[dict, merge_receipts]
    plan: dict | None
    plan_revision: int
    plan_digest: str
    plan_edits: int
    approved_digest: str
    repair_rounds: int
    review_rounds: int
    review_current: bool
    findings: list


def required_capabilities(request: WorkflowRequest) -> set[str]:
    needed = {"jobs.write", "verify", "events", "cancel"}
    if request.plan is None or request.reviewer:
        needed.add("jobs.read")
    return needed


def _stop(update: dict, receipt, here: str) -> Command:
    """Terminal stops finish; resumable ones park on the same node."""
    stopped = stop_for(receipt.status, receipt.detail, receipt.operation_id)
    goto = here if stopped["status"] in PARKING else "finish"
    return Command(update={**update, **stopped}, goto=goto)


def build(rt: Runtime) -> StateGraph:
    def guard(state: dict, phase: str) -> Command | None:
        if rt.cancelled(state):
            return Command(update={"status": "cancel_requested", "blocker": "cancelled",
                                   "phase": phase}, goto="finish")
        rt.emit(state, "workflow.phase", key=f"{phase}:{state.get('repair_rounds', 0)}",
                payload={"phase": phase}, diagnostic=True)
        return None

    def validate(state: State) -> Command:
        if stopped := guard(state, "validate"):
            return stopped
        request = WorkflowRequest.from_dict(state["request"])
        missing = required_capabilities(request) - rt.host.capabilities()
        admission = rt.host.admit(request) if not missing else None
        if admission is not None and admission.plan_approval and "decisions" not in (
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
        limits = effective_limits(admission.limits, request.limits)
        update: dict[str, Any] = {
            "admission": admission.to_dict(), "limits": limits, "phase": "validate",
            "status": "running", "blocker": "", "detail": "",
        }
        rt.emit(state, "workflow.started", key="started",
                payload={"workflow": request.workflow, "reviewer": request.reviewer,
                         "checks": len(request.checks), "supplied_plan": request.plan is not None})
        if request.plan is not None:
            try:
                plan = validate_plan(request.plan, limits["max_plan_steps"])
            except ContractError as error:
                return Command(update={**update, "status": "blocked", "blocker": "invalid_plan",
                                       "detail": str(error)}, goto="finish")
            update.update(plan=plan, plan_revision=1, plan_digest=digest(plan))
            return Command(update=update, goto="approve")
        return Command(update=update, goto="inspect")

    def inspect(state: State) -> Command:
        if stopped := guard(state, "inspect"):
            return stopped
        receipt, update = rt.run_job(state, spec(
            state, "inspect", "read", "inspect", state["request"]["goal"]))
        update["phase"] = "inspect"
        if receipt.status != "settled":
            if receipt.status == "failed":
                return Command(update={**update, "status": "failed", "blocker": "inspect_failed"},
                               goto="finish")
            return _stop(update, receipt, "inspect")
        return Command(update={**update, "status": "running"}, goto="plan")

    def plan(state: State) -> Command:
        if stopped := guard(state, "plan"):
            return stopped
        context = state["jobs"].get(f"{state['request']['attempt_id']}/inspect", {})
        receipt, update = rt.run_job(state, spec(
            state, "plan", "read", "plan", state["request"]["goal"],
            {"context_job": context.get("job_id", ""), "checks": list(state["request"]["checks"]),
             "max_steps": state["limits"]["max_plan_steps"]}))
        update["phase"] = "plan"
        if receipt.status != "settled":
            if receipt.status == "failed":
                return Command(update={**update, "status": "failed", "blocker": "plan_failed"},
                               goto="finish")
            return _stop(update, receipt, "plan")
        try:
            proposed = validate_plan(receipt.output.get("plan"), state["limits"]["max_plan_steps"])
        except ContractError as error:
            return Command(update={**update, "status": "needs_review", "blocker": "invalid_plan",
                                   "detail": str(error)}, goto="finish")
        return Command(update={**update, "status": "running", "plan": proposed,
                               "plan_revision": 1, "plan_digest": digest(proposed)},
                       goto="approve")

    def approve(state: State) -> Command:
        if stopped := guard(state, "approve"):
            return stopped
        if not state["admission"]["plan_approval"]:
            return Command(update={"phase": "approve", "approved_digest": state["plan_digest"]},
                           goto="implement")
        request = state["request"]
        pending = PendingDecision(
            decision_id=f"{request['attempt_id']}:plan:{state['plan_revision']}",
            run_id=request["run_id"], attempt_id=request["attempt_id"], kind="plan_approval",
            revision=state["plan_revision"], digest=state["plan_digest"],
            summary="; ".join(s["title"] for s in state["plan"]["steps"])[:2000],
            options=("approve", "deny", "edit"),
        )
        rt.emit(state, "decision.pending", key=pending.decision_id,
                payload={"decision_id": pending.decision_id, "kind": pending.kind,
                         "revision": pending.revision, "summary": pending.summary,
                         "options": list(pending.options)})
        # Everything above is deterministic from state, so re-entry after a
        # restart recreates the identical decision.
        answer = interrupt(pending.to_dict())
        response = DecisionResponse.from_dict(answer)
        if (response.decision_id, response.revision, response.digest) != (
            pending.decision_id, pending.revision, pending.digest
        ):
            return Command(update={"status": "blocked", "blocker": "decision_mismatch"},
                           goto="finish")
        rt.emit(state, "decision.resolved", key=pending.decision_id,
                payload={"decision_id": pending.decision_id, "choice": response.choice})
        if response.choice == "approve":
            return Command(update={"phase": "approve", "approved_digest": state["plan_digest"]},
                           goto="implement")
        if response.choice == "deny":
            return Command(update={"status": "denied", "blocker": "plan_denied"}, goto="finish")
        edits = state.get("plan_edits", 0) + 1
        if edits > state["limits"]["max_plan_edits"]:
            return Command(update={"status": "needs_review", "blocker": "plan_edit_limit"},
                           goto="finish")
        edited = validate_plan(response.edited_plan, state["limits"]["max_plan_steps"])
        # An edit is a new plan revision; it needs its own approval.
        return Command(update={"plan": edited, "plan_revision": state["plan_revision"] + 1,
                               "plan_digest": digest(edited), "plan_edits": edits},
                       goto="approve")

    def implement(state: State) -> Command:
        if stopped := guard(state, "implement"):
            return stopped
        if state.get("approved_digest") != state["plan_digest"]:
            return Command(update={"status": "blocked", "blocker": "plan_not_approved"},
                           goto="finish")
        receipt, update = rt.run_job(state, spec(
            state, "write", "write", f"implement-{state['plan_digest'][:16]}",
            state["request"]["goal"],
            {"plan": state["plan"], "plan_digest": state["plan_digest"],
             "plan_revision": state["plan_revision"]}))
        update["phase"] = "implement"
        if receipt.status == "failed":
            return Command(update={**update, "status": "failed", "blocker": "implement_failed",
                                   "detail": receipt.detail}, goto="finish")
        if receipt.status != "settled":
            return _stop(update, receipt, "implement")
        return Command(update={**update, "status": "running", "review_current": False},
                       goto="verify")

    def verify(state: State) -> Command:
        if stopped := guard(state, "verify"):
            return stopped
        request = WorkflowRequest.from_dict(state["request"])
        report = rt.host.verify(request, request.checks, final=False).to_dict()
        outcome, blocker = check_evidence(request.checks, report)
        verification = {**report, "outcome": outcome, "blocker": blocker, "final": False}
        rt.emit(state, "verification.result",
                key=f"verify:{state.get('repair_rounds', 0)}:{state.get('review_rounds', 0)}",
                payload={"outcome": outcome, "blocker": blocker, "final": False,
                         "checks": [[r["check_id"], r["state"]] for r in report["results"]]})
        update = {"phase": "verify", "verification": verification}
        if outcome == "passed":
            needs_review = request.reviewer and not state.get("review_current")
            return Command(update={**update, "status": "running"},
                           goto="review" if needs_review else "finalize")
        if outcome == "failed":
            if state.get("repair_rounds", 0) < state["limits"]["max_repair_rounds"]:
                return Command(update={**update, "status": "running"}, goto="repair")
            return Command(update={**update, "status": "needs_review",
                                   "blocker": "repairs_exhausted"}, goto="finish")
        return Command(update={**update, "status": "needs_review", "blocker": blocker},
                       goto="finish")

    def repair(state: State) -> Command:
        if stopped := guard(state, "repair"):
            return stopped
        rounds = state.get("repair_rounds", 0) + 1
        failing = [r["check_id"] for r in state["verification"]["results"]
                   if r["state"] != "passed"]
        receipt, update = rt.run_job(state, spec(
            state, "write", "write", f"repair-{rounds}", "Repair the failing checks",
            {"failing_checks": failing, "findings": state.get("findings", []),
             "plan_digest": state["plan_digest"]}))
        update.update(phase="repair")
        if receipt.status == "failed":
            return Command(update={**update, "status": "needs_review",
                                   "blocker": "repair_failed"}, goto="finish")
        if receipt.status != "settled":
            return _stop(update, receipt, "repair")
        update.update(repair_rounds=rounds, review_current=False, findings=[])
        if not receipt.changed_files:
            return Command(update={**update, "status": "needs_review", "blocker": "no_progress"},
                           goto="finish")
        return Command(update={**update, "status": "running"}, goto="verify")

    def review(state: State) -> Command:
        if stopped := guard(state, "review"):
            return stopped
        rounds = state.get("review_rounds", 0) + 1
        if rounds > state["limits"]["max_review_rounds"]:
            return Command(update={"status": "needs_review", "blocker": "review_rounds_exhausted"},
                           goto="finish")
        receipt, update = rt.run_job(state, spec(
            state, "review", "read", f"review-{rounds}", "Review the change against the plan",
            {"plan_digest": state["plan_digest"], "repair_rounds": state.get("repair_rounds", 0),
             "verification": [[r["check_id"], r["state"], r.get("receipt_id", "")]
                              for r in state["verification"]["results"]]}))
        update.update(phase="review", review_rounds=rounds)
        if receipt.status == "failed":
            return Command(update={**update, "status": "needs_review",
                                   "blocker": "review_failed"}, goto="finish")
        if receipt.status != "settled":
            return _stop(update, receipt, "review")
        verdict = receipt.output.get("verdict")
        findings = receipt.output.get("findings", [])
        if verdict == "approve":
            return Command(update={**update, "status": "running", "review_current": True},
                           goto="finalize")
        if verdict == "changes_requested" and isinstance(findings, list):
            if state.get("repair_rounds", 0) < state["limits"]["max_repair_rounds"]:
                return Command(update={**update, "status": "running",
                                       "findings": findings[:32]}, goto="repair")
            return Command(update={**update, "status": "needs_review",
                                   "blocker": "review_unresolved"}, goto="finish")
        return Command(update={**update, "status": "needs_review", "blocker": "review_invalid"},
                       goto="finish")

    def finalize(state: State) -> Command:
        if stopped := guard(state, "finalize"):
            return stopped
        request = WorkflowRequest.from_dict(state["request"])
        report = rt.host.verify(request, request.checks, final=True).to_dict()
        outcome, blocker = check_evidence(request.checks, report)
        rt.emit(state, "verification.result", key="final",
                payload={"outcome": outcome, "blocker": blocker, "final": True,
                         "checks": [[r["check_id"], r["state"]] for r in report["results"]]})
        verification = {**report, "outcome": outcome, "blocker": blocker, "final": True}
        if outcome == "passed":
            return Command(update={"phase": "finalize", "verification": verification,
                                   "status": "verified", "blocker": ""}, goto="finish")
        return Command(update={"phase": "finalize", "verification": verification,
                               "status": "needs_review",
                               "blocker": "final_checks_failed" if outcome == "failed"
                               else blocker}, goto="finish")

    def finish(state: State) -> dict:
        return rt.finish(state)

    graph = StateGraph(State)
    for name, fn in (("validate", validate), ("inspect", inspect), ("plan", plan),
                     ("approve", approve), ("implement", implement), ("verify", verify),
                     ("repair", repair), ("review", review), ("finalize", finalize)):
        graph.add_node(name, bounded(fn))
    graph.add_node("finish", finish)
    graph.add_edge(START, "validate")
    graph.add_edge("finish", END)
    return graph
