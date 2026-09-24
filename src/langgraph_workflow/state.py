"""Checkpointed graph state and the helpers every workflow node shares.

State holds only small plain data: identities, frozen admission, references
to host receipts, counters, and a bounded plan/result. Clients, closures,
locks and transcripts never enter it; the host port is bound in node closures.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.types import Command

from .contracts import JobReceipt, JobSpec, canonical
from .events import make_event
from .ports import WorkflowHost

# Never started, so submitting again cannot duplicate an effect: the host
# either starts an admitted operation, reattaches to a running one, or
# re-evaluates a refusal it did not act on.
RESUBMITTABLE = ("admitted", "running", "budget_exhausted", "busy")

_RANK = {
    "settled": 9, "failed": 8, "denied": 8, "cancelled": 7, "budget_exhausted": 7,
    "busy": 5, "uncertain": 4, "running": 2, "admitted": 1,
}


def merge_receipts(left: dict | None, right: dict | None) -> dict:
    """Fan-in reducer keyed by operation id.

    Commutative, associative and idempotent: a replayed or reordered branch
    can neither duplicate an entry nor let a less-settled outcome overwrite a
    settled one. Ties break on canonical JSON, never on arrival order.
    """
    merged = dict(left or {})
    for key, new in (right or {}).items():
        old = merged.get(key)
        if old is None or (_RANK[new["status"]], canonical(new)) > (
            _RANK[old["status"]], canonical(old)
        ):
            merged[key] = new
    return dict(sorted(merged.items()))


class BaseState(TypedDict, total=False):
    request: dict  # frozen WorkflowRequest
    versions: dict  # definition / schema / contract versions at admission
    admission: dict  # frozen host Admission (policy ref, approval policy)
    limits: dict  # effective limits, frozen at admission
    phase: str
    status: str
    blocker: str
    detail: str
    jobs: Annotated[dict, merge_receipts]
    job_count: Annotated[int, operator.add]  # admitted jobs; never reset on resume
    transitions: Annotated[int, operator.add]  # node executions, bounded by max_transitions
    verification: dict | None
    result: dict
    routing: dict


class Runtime:
    """Per-execution bindings. Lives in node closures, never in state."""

    def __init__(self, host: WorkflowHost, controls) -> None:
        self.host = host
        self.controls = controls  # callable(attempt_id) -> {"pause","cancel"}

    def cancelled(self, state: dict) -> bool:
        return self.controls(state["request"]["attempt_id"])["cancel"]

    def emit(self, state: dict, kind: str, *, key: str = "", **extra: Any) -> None:
        get_stream_writer()(make_event(kind, state["request"], key=key, **extra))

    def run_job(self, state: dict, spec: JobSpec) -> tuple[JobReceipt, dict]:
        """Execute one host job safely on (re-)entry.

        Returns the receipt and the state update. A known operation is looked
        up, never resubmitted; an uncertain one is surfaced, never replayed.
        """
        new = spec.operation_id not in (state.get("jobs") or {})
        if new and state.get("job_count", 0) >= state["limits"]["max_jobs"]:
            receipt = JobReceipt(spec.operation_id, "", "budget_exhausted",
                                 spec.input_fingerprint, detail="graph max_jobs reached")
            return receipt, {}
        receipt = self.host.lookup(spec.operation_id)
        if receipt is not None and receipt.input_fingerprint != spec.input_fingerprint:
            receipt = JobReceipt(spec.operation_id, receipt.job_id, "uncertain",
                                 spec.input_fingerprint, detail="operation_conflict")
        elif receipt is None or receipt.status in RESUBMITTABLE:
            self.emit(state, "job.submitted", key="submit", operation_id=spec.operation_id,
                      parent_id=spec.parent_id, payload={"kind": spec.kind, "access": spec.access})
            receipt = self.host.execute(spec)
        if spec.access == "read" and receipt.changed_files:
            receipt = JobReceipt(receipt.operation_id, receipt.job_id, "uncertain",
                                 receipt.input_fingerprint, changed_files=receipt.changed_files,
                                 detail="read_only_violation")
        self.emit(state, "job.outcome", key=receipt.status, operation_id=spec.operation_id,
                  parent_id=spec.parent_id,
                  payload={"kind": spec.kind, "status": receipt.status, "job_id": receipt.job_id,
                           "changed_files": len(receipt.changed_files), "detail": receipt.detail,
                           "usage": receipt.usage})
        update = {"jobs": {spec.operation_id: receipt.to_dict()}}
        if new:
            update["job_count"] = 1
        return receipt, update

    def finish(self, state: dict) -> dict:
        """Single terminal node: settle cancellation and publish the outcome."""
        status, blocker = state.get("status") or "failed", state.get("blocker", "")
        if status == "running":
            status, blocker = "failed", "internal_route"
        if status == "cancel_requested" and self.host.cancel(state["request"]["attempt_id"]):
            status = "cancelled"
        verification = state.get("verification") or {}
        result = {
            "status": status,
            "blocker": blocker,
            "plan_digest": state.get("plan_digest", ""),
            "verification": verification.get("outcome", ""),
            "evidence": [r.get("receipt_id", "") for r in verification.get("results", ())],
            "jobs": {k: v["status"] for k, v in (state.get("jobs") or {}).items()},
        }
        if state.get("synthesis"):
            result["synthesis_job"] = state["synthesis"].get("job_id", "")
            result["artifacts"] = state["synthesis"].get("artifacts", [])
        self.emit(state, "workflow.outcome", key=status,
                  payload={"status": status, "blocker": blocker, "detail": state.get("detail", ""),
                           "verification": result["verification"]})
        return {"status": status, "blocker": blocker, "phase": "finish", "result": result}


def bounded(node):
    """Count node executions against the effective ``max_transitions``.

    LangGraph's recursion limit is fixed when a run starts, before host
    admission can narrow limits; this counter applies the frozen limit.
    """

    def wrapper(state: dict):
        if state.get("transitions", 0) >= state["limits"]["max_transitions"]:
            return Command(update={"status": "failed", "blocker": "transition_limit"},
                           goto="finish")
        result = node(state)
        return Command(update={**(result.update or {}), "transitions": 1}, goto=result.goto)

    wrapper.__name__ = node.__name__
    return wrapper


def spec(state: dict, kind: str, access: str, key: str, instruction: str,
         inputs: dict | None = None, parent: str = "") -> JobSpec:
    request = state["request"]
    return JobSpec(
        operation_id=f"{request['attempt_id']}/{key}",
        run_id=request["run_id"],
        attempt_id=request["attempt_id"],
        kind=kind,
        access=access,
        instruction=instruction,
        inputs=inputs or {},
        parent_id=parent,
        route_ref=request.get("route_ref", ""),
        checkout_ref=request.get("checkout_ref", ""),
    )


# Receipt status -> (attempt status, blocker) for outcomes that stop a workflow.
STOPPING_RECEIPTS = {
    "denied": ("needs_review", "job_denied"),
    "cancelled": ("cancel_requested", "cancelled"),
    "budget_exhausted": ("budget_exhausted", "budget_exhausted"),
    "busy": ("waiting_for_capability", "workspace_busy"),
    "uncertain": ("uncertain", "uncertain_action"),
    "admitted": ("uncertain", "uncertain_action"),
    "running": ("uncertain", "uncertain_action"),
}


def stop_for(status: str, detail: str, operation_id: str) -> dict | None:
    """Attempt status for a receipt outcome that stops the workflow."""
    if status not in STOPPING_RECEIPTS:
        return None
    attempt_status, blocker = STOPPING_RECEIPTS[status]
    if detail in ("read_only_violation", "operation_conflict"):
        # Invariant violations need a human; they never park for automatic retry.
        attempt_status, blocker = "blocked", detail
    return {"status": attempt_status, "blocker": blocker,
            "detail": f"{operation_id}: {detail or status}"}


def check_evidence(checks: tuple | list, report: dict) -> tuple[str, str]:
    """Package-side floor over a host report: every declared check needs a
    ``passed`` result with a receipt id before anything counts as verified."""
    by_id = {r["check_id"]: r for r in report.get("results", ())}
    states = []
    for check in checks:
        result = by_id.get(check["id"])
        if result is None:
            states.append("missing")
        elif result["state"] == "passed" and not result.get("receipt_id"):
            states.append("missing")
        else:
            states.append(result["state"])
    if not checks:
        return "needs_review", "no_checks_declared"
    if "failed" in states:
        return "failed", "checks_failed"
    for state, blocker in (("missing", "missing_evidence"), ("stale", "stale_evidence"),
                           ("denied", "check_denied"), ("unsupported", "unsupported_check"),
                           ("needs_review", "human_review_required")):
        if state in states:
            return "needs_review", blocker
    if report.get("status") != "passed":
        return "needs_review", "host_not_passed"
    return "passed", ""
