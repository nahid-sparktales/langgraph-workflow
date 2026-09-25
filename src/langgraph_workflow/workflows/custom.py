"""User-drawn graphs, run by one interpreter node.

The request pins a validated definition (``definitions.validate_definition``).
The LangGraph graph itself is fixed: ``step`` runs whichever definition step
is current and routes to the next, ``branch`` runs parallel read-only
branches (``Send``) that meet again in ``step`` at their join. Keeping the
compiled graph independent of the definition means resume, checkpoints and
encryption work exactly as for the built-in workflows, and no user data ever
becomes code.

Loops are bounded by each step's ``max_visits`` and by the run's
``max_transitions`` and ``max_jobs``. A run ends ``verified`` only at an end
step reached after a check step passed with no write step since.
"""

from __future__ import annotations

from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from ..contracts import DecisionResponse, PendingDecision, WorkflowRequest, digest
from ..definitions import IMPLICIT, MAX_VISITS, targets
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

DEFINITION_VERSION = "custom/1"
CONTEXT_OUTPUTS = 3  # earlier step results handed to the next agent step


def _merge(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class State(BaseState, total=False):
    jobs: Annotated[dict, merge_receipts]
    node: str  # current definition step
    visits: dict  # step id -> times entered
    outputs: Annotated[dict, _merge]  # step id -> last result summary
    order: list  # step ids in the order they finished, for context
    last_check: dict | None
    writes_since_check: bool


def required_capabilities(request: WorkflowRequest) -> set[str]:
    types = {n["type"]: n for n in request.definition["nodes"]}
    needed = {"events", "cancel"}
    accesses = {n.get("access") for n in request.definition["nodes"] if n["type"] == "task"}
    needed |= {f"jobs.{access}" for access in accesses}
    if "check" in types:
        needed.add("verify")
    if "approval" in types:
        needed.add("decisions")
    return needed


def build(rt: Runtime) -> StateGraph:
    def definition(state: dict) -> dict:
        return state["request"]["definition"]

    def node(state: dict, node_id: str) -> dict:
        return next(n for n in definition(state)["nodes"] if n["id"] == node_id)

    def enter(state: dict, target: str, update: dict) -> Command:
        """Route into ``target``, counting the visit against its bound."""
        visits = dict(state.get("visits") or {})
        visits[target] = visits.get(target, 0) + 1
        limit = node(state, target).get("max_visits", MAX_VISITS)
        if visits[target] > limit:
            return Command(update={**update, "status": "needs_review", "blocker": "loop_limit",
                                   "detail": f"{node(state, target)['title']} ran {limit} times"},
                           goto="finish")
        return Command(update={**update, "visits": visits, "node": target}, goto="step")

    def follow(state: dict, current: dict, port: str, update: dict) -> Command:
        nxt = targets(definition(state), current["id"], port)
        if nxt:
            return enter(state, nxt[0], update)
        status, blocker = IMPLICIT.get((current["type"], port), ("failed", "internal_route"))
        return Command(update={**update, "status": status, "blocker": blocker,
                               "detail": current["title"]}, goto="finish")

    def context(state: dict) -> list:
        outputs = state.get("outputs") or {}
        return [outputs[i] for i in (state.get("order") or [])[-CONTEXT_OUTPUTS:] if i in outputs]

    def task_spec(state: dict, current: dict, visit: int, parent: str = ""):
        agent = current.get("agent") or definition(state).get("agent", "")
        return spec(state, "task", current["access"], f"{current['id']}-{visit}",
                    current["instruction"],
                    {"step": current["id"], "title": current["title"],
                     "goal": state["request"]["goal"], "choices": current["choices"],
                     "agent_name": current.get("agent_name") or definition(state).get(
                         "agent_name", ""),
                     "context": context(state)},
                    parent=parent, assignee=agent)

    def summary(receipt, current: dict) -> dict:
        out = receipt.output
        return {"step": current["id"], "title": current["title"], "job_id": receipt.job_id,
                "status": receipt.status, "detail": receipt.detail[:500],
                "summary": str(out.get("summary", ""))[:2000],
                "choice": out.get("choice") if isinstance(out.get("choice"), str) else None,
                "changed_files": len(receipt.changed_files)}

    def validate(state: State) -> Command:
        if rt.cancelled(state):
            return Command(update={"status": "cancel_requested", "blocker": "cancelled"},
                           goto="finish")
        request = WorkflowRequest.from_dict(state["request"])
        missing = required_capabilities(request) - rt.host.capabilities()
        admission = rt.host.admit(request) if not missing else None
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
                payload={"workflow": "custom", "definition": request.definition["id"],
                         "steps": len(request.definition["nodes"]),
                         "checks": len(request.checks)})
        start = next(n for n in request.definition["nodes"] if n["type"] == "start")
        return follow(state, start, "next", {
            "admission": admission.to_dict(), "phase": "validate",
            "limits": effective_limits(admission.limits, request.limits),
            "status": "running", "blocker": "", "detail": "", "outputs": {}, "order": [],
            "writes_since_check": False})

    def step(state: State) -> Command:
        current = node(state, state["node"])
        phase = current["id"]
        if rt.cancelled(state):
            return Command(update={"status": "cancel_requested", "blocker": "cancelled",
                                   "phase": phase}, goto="finish")
        rt.emit(state, "workflow.phase", key=f"{phase}:{state['visits'].get(phase, 0)}",
                payload={"phase": phase}, diagnostic=True)
        return {"task": task, "approval": approval, "check": check, "split": split,
                "join": join, "end": end}[current["type"]](state, current)

    def task(state: dict, current: dict) -> Command:
        visit = state["visits"][current["id"]]
        receipt, update = rt.run_job(state, task_spec(state, current, visit))
        update["phase"] = current["id"]
        if current["access"] == "write" and receipt.changed_files:
            update["writes_since_check"] = True  # also when the step then fails
        if receipt.status == "failed":
            update["outputs"] = {current["id"]: summary(receipt, current)}
            return follow(state, current, "failed", update)
        if receipt.status != "settled":
            stopped = stop_for(receipt.status, receipt.detail, receipt.operation_id)
            return Command(update={**update, **stopped},
                           goto="step" if stopped["status"] in PARKING else "finish")
        result = summary(receipt, current)
        update.update(outputs={current["id"]: result}, status="running",
                      order=[*(state.get("order") or []), current["id"]][-32:])
        port = "done"
        if current["choices"]:
            port = result["choice"]
            if port not in current["choices"]:
                return Command(update={**update, "status": "needs_review",
                                       "blocker": "invalid_choice",
                                       "detail": f"{current['title']}: expected one of "
                                                 f"{', '.join(current['choices'])}"},
                               goto="finish")
        return follow(state, current, port, update)

    def approval(state: dict, current: dict) -> Command:
        request, visit = state["request"], state["visits"][current["id"]]
        shown = context(state)[-1:]  # what is being approved: the last step's result
        text = current["prompt"] or current["title"]
        if shown and shown[0]["summary"]:
            text = f"{text}\n\n{shown[0]['summary']}"
        pending = PendingDecision(
            decision_id=f"{request['attempt_id']}:{current['id']}:{visit}",
            run_id=request["run_id"], attempt_id=request["attempt_id"], kind="approval",
            revision=visit, digest=digest([current["id"], visit, text]),
            summary=text[:2000], options=("approve", "decline"))
        rt.emit(state, "decision.pending", key=pending.decision_id,
                payload={"decision_id": pending.decision_id, "kind": pending.kind,
                         "summary": pending.summary, "options": list(pending.options)})
        response = DecisionResponse.from_dict(interrupt(pending.to_dict()))
        if (response.decision_id, response.revision, response.digest) != (
                pending.decision_id, pending.revision, pending.digest):
            return Command(update={"status": "blocked", "blocker": "decision_mismatch"},
                           goto="finish")
        rt.emit(state, "decision.resolved", key=pending.decision_id,
                payload={"decision_id": pending.decision_id, "choice": response.choice})
        port = "approved" if response.choice == "approve" else "declined"
        return follow(state, current, port, {"phase": current["id"]})

    def check(state: dict, current: dict) -> Command:
        request = WorkflowRequest.from_dict(state["request"])
        report = (rt.host.verify(request, request.checks, final=False).to_dict()
                  if request.checks else {"status": "needs_review", "results": []})
        outcome, blocker = check_evidence(request.checks, report)
        visit = state["visits"][current["id"]]
        rt.emit(state, "verification.result", key=f"{current['id']}:{visit}",
                payload={"outcome": outcome, "blocker": blocker, "final": False,
                         "checks": [[r["check_id"], r["state"]] for r in report["results"]]})
        update = {"phase": current["id"], "status": "running",
                  "verification": {**report, "outcome": outcome, "blocker": blocker},
                  "last_check": {"step": current["id"], "outcome": outcome},
                  "writes_since_check": False}
        port = {"passed": "passed", "failed": "failed"}.get(outcome, "needs_review")
        if port == "needs_review" and not targets(definition(state), current["id"], port):
            return Command(update={**update, "status": "needs_review", "blocker": blocker},
                           goto="finish")
        return follow(state, current, port, update)

    def split_of(state: dict, join_id: str) -> dict:
        d = definition(state)
        return next(n for n in d["nodes"] if n["type"] == "split" and targets(
            d, targets(d, n["id"], "next")[0], "done") == [join_id])

    def branches(state: dict, split_node: dict) -> list[dict]:
        return [node(state, b) for b in targets(definition(state), split_node["id"], "next")]

    def send(state: dict, branch: dict, join_id: str, visit: int) -> Send:
        return Send("branch", {"request": state["request"], "limits": state["limits"],
                               "jobs": state.get("jobs", {}),
                               "job_count": state.get("job_count", 0),
                               "outputs": state.get("outputs", {}),
                               "order": state.get("order", []),
                               "branch": branch["id"], "parent": join_id, "visit": visit})

    def split(state: dict, current: dict) -> Command:
        parts = branches(state, current)
        join_id = targets(definition(state), parts[0]["id"], "done")[0]
        visit = state["visits"][current["id"]]
        todo = [b for b in parts
                if f"{state['request']['attempt_id']}/{b['id']}-{visit}" not in state["jobs"]]
        if state.get("job_count", 0) + len(todo) > state["limits"]["max_jobs"]:
            return Command(update={"phase": current["id"], "status": "budget_exhausted",
                                   "blocker": "budget_exhausted"}, goto="step")
        rt.emit(state, "workflow.routing", key=current["id"],
                payload={"mode": "parallel", "branches": [b["id"] for b in parts],
                         "max_parallel": state["limits"]["max_parallel_reads"]})
        visits = {**state["visits"], join_id: state["visits"].get(join_id, 0) + 1}
        return Command(update={"phase": current["id"], "node": join_id, "visits": visits},
                       goto=[send(state, b, join_id, visit) for b in parts])

    def branch(payload: dict) -> dict:
        if rt.cancelled(payload):
            return {}  # parallel branches never write shared scalars; join stops
        current = node(payload, payload["branch"])
        receipt, update = rt.run_job(payload, task_spec(payload, current, payload["visit"],
                                                        parent=payload["parent"]))
        return {**update, "outputs": {current["id"]: summary(receipt, current)}}

    def join(state: dict, current: dict) -> Command:
        owner = split_of(state, current["id"])
        parts, visit = branches(state, owner), state["visits"][owner["id"]]
        outputs = state.get("outputs") or {}
        results = [outputs.get(b["id"], {"status": "missing", "detail": "",
                                         "step": b["id"]}) for b in parts]
        for result in results:
            if result["status"] == "settled":
                continue
            stopped = stop_for(result["status"], result["detail"], result["step"]) or {
                "status": "needs_review", "blocker": "branch_failed", "detail": result["step"]}
            if stopped["status"] in PARKING or result["status"] == "missing":
                redo = [b for b in parts if outputs.get(b["id"], {}).get("status") != "settled"]
                return Command(update={"phase": current["id"], **stopped},
                               goto=[send(state, b, current["id"], visit) for b in redo])
            return Command(update={"phase": current["id"], **stopped}, goto="finish")
        order = [*(state.get("order") or []), *(b["id"] for b in parts)][-32:]
        return follow(state, current, "next", {"phase": current["id"], "status": "running",
                                               "order": order})

    def end(state: dict, current: dict) -> Command:
        last = state.get("last_check")
        if last and last["outcome"] == "passed" and not state.get("writes_since_check"):
            status, blocker = "verified", ""
        elif last is None:
            status, blocker = "needs_review", "no_checks_run"
        elif state.get("writes_since_check"):
            status, blocker = "needs_review", "changed_after_checks"
        else:
            status, blocker = "needs_review", last["outcome"]
        return Command(update={"phase": current["id"], "status": status, "blocker": blocker},
                       goto="finish")

    def finish(state: State) -> dict:
        return rt.finish(state)

    graph = StateGraph(State)
    graph.add_node("validate", bounded(validate))
    graph.add_node("step", bounded(step))
    graph.add_node("branch", branch)  # Send target: bounded by the split's branch count
    graph.add_node("finish", finish)
    graph.add_edge(START, "validate")
    graph.add_edge("branch", "step")
    graph.add_edge("finish", END)
    return graph
