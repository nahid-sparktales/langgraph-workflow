"""User-drawn graphs: validation rejects unbounded or ambiguous shapes, and
the interpreter runs them with the same durability as built-in workflows."""

import copy

import pytest
from conftest import CHECK, respond

from langgraph_workflow import WorkflowExecutor
from langgraph_workflow.agent_host import AgentHost, ReportRejected
from langgraph_workflow.contracts import ContractError
from langgraph_workflow.definitions import validate_definition


def graph(nodes, edges, **extra):
    return {"id": "flow", "title": "Flow", "nodes": nodes,
            "edges": [dict(zip(("from", "on", "to"), e, strict=True)) for e in edges], **extra}


def task(node_id, access="read", **extra):
    return {"id": node_id, "type": "task", "title": node_id.title(), "access": access,
            "instruction": f"Do {node_id}", **extra}


START, END = {"id": "start", "type": "start"}, {"id": "end", "type": "end"}

CHANGE = graph(
    [START, task("plan", agent="planner-uuid", agent_name="Nova"),
     {"id": "ok", "type": "approval", "title": "Approve the plan"},
     task("build", "write", agent="builder-uuid", agent_name="Kai", max_visits=2),
     {"id": "check", "type": "check", "max_visits": 2}, END],
    [("start", "next", "plan"), ("plan", "done", "ok"), ("ok", "approved", "build"),
     ("build", "done", "check"), ("check", "passed", "end"), ("check", "failed", "build")])

PARALLEL = graph(
    [START, {"id": "fan", "type": "split"}, task("a"), task("b"), {"id": "meet", "type": "join"},
     task("sum"), END],
    [("start", "next", "fan"), ("fan", "next", "a"), ("fan", "next", "b"), ("a", "done", "meet"),
     ("b", "done", "meet"), ("meet", "next", "sum"), ("sum", "done", "end")])


def custom_request(definition, attempt="cus-1", **over):
    return {"workflow": "custom", "run_id": "run-3", "task_id": "task-3", "attempt_id": attempt,
            "workspace_id": "ws-1", "goal": "Make result.txt say done", "checks": [CHECK],
            "definition": definition, **over}


@pytest.fixture
def agent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    made = []

    def factory():
        host = AgentHost(tmp_path / "data", workspace)
        ex = WorkflowExecutor(host, tmp_path / "data" / "checkpoints.sqlite3")
        made.append(ex)
        return host, ex

    yield workspace, factory
    for ex in made:
        ex.close()


def report(host, job, result):
    """What the assigned agent's chat does after the window hands it the job."""
    claim = host.dispatch(job["operation_id"])["claim"] if job["assignee"] else ""
    return host.report(job["operation_id"], "completed", result, claim=claim)


def jobs(host, status):
    assert status.status == "waiting_for_job", status
    return host.pending(status.attempt_id)


def test_valid_definitions_are_normalized():
    out = validate_definition(CHANGE)
    assert out["schema"] == "lgw.graph/1"
    assert [n["max_visits"] for n in out["nodes"] if n["type"] == "check"] == [2]
    assert validate_definition(out) == out  # idempotent: a pinned copy re-validates
    validate_definition(PARALLEL)


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["nodes"].append({"id": "s2", "type": "start"}), "exactly one start"),
    (lambda d: d["edges"].pop(0), "connect the 'next'"),
    (lambda d: d["nodes"][1].update(code="__import__('os')"), "unknown fields"),
    (lambda d: d["nodes"][1].update(type="python"), "step type"),
    (lambda d: d["nodes"][3].update(max_visits=50), "max visits"),
    (lambda d: d["edges"].append({"from": "plan", "on": "done", "to": "end"}), "two connections"),
    (lambda d: d["edges"].append({"from": "build", "on": "done", "to": "start"}), "into the start"),
    (lambda d: d["nodes"].extend([task("trap"), task("trap2")]) or d["edges"].extend([
        {"from": "ok", "on": "declined", "to": "trap"},
        {"from": "trap", "on": "done", "to": "trap2"},
        {"from": "trap2", "on": "done", "to": "trap"}]), "no path to an end"),
    (lambda d: d["nodes"].append(task("orphan")) or d["edges"].append(
        {"from": "orphan", "on": "done", "to": "end"}), "not reachable"),
])
def test_invalid_definitions_are_rejected(mutate, message):
    broken = copy.deepcopy(CHANGE)
    mutate(broken)
    with pytest.raises(ContractError, match=message):
        validate_definition(broken)


def test_parallel_branches_must_be_read_only_and_meet_at_one_join():
    writer = copy.deepcopy(PARALLEL)
    writer["nodes"][2]["access"] = "write"
    with pytest.raises(ContractError, match="read-only"):
        validate_definition(writer)
    leak = copy.deepcopy(PARALLEL)
    leak["edges"][3] = {"from": "a", "on": "done", "to": "sum"}
    with pytest.raises(ContractError, match="lead only to a join"):
        validate_definition(leak)


def test_a_drawn_change_loops_until_checks_pass_across_restarts(agent):
    workspace, make = agent
    host, ex = make()
    [job] = jobs(host, ex.start(custom_request(CHANGE)))
    assert (job["kind"], job["access"], job["assignee"]) == ("task", "read", "planner-uuid")
    assert job["inputs"]["agent_name"] == "Nova"
    with pytest.raises(ReportRejected, match="another agent"):  # not handed over yet
        host.report(job["operation_id"], "completed", {"summary": "sneaky"})
    first = host.dispatch(job["operation_id"])
    again = host.dispatch(job["operation_id"])  # a retried hand-off keeps its claim
    assert (first["first"], again["first"], again["claim"]) == (True, False, first["claim"])
    with pytest.raises(ReportRejected, match="another agent"):
        host.report(job["operation_id"], "completed", {}, claim="guess")
    report(host, job, {"summary": "1. Write result.txt"})
    waiting = ex.resume("cus-1")
    assert waiting.pending_decision["kind"] == "approval"
    assert "Write result.txt" in waiting.pending_decision["summary"]

    host, ex = make()  # restart between approval and the build
    [build] = jobs(host, ex.decide(respond(waiting)))
    assert (build["access"], build["assignee"]) == ("write", "builder-uuid")
    assert build["inputs"]["context"][-1]["summary"] == "1. Write result.txt"
    (workspace / "result.txt").write_text("not yet\n")
    report(host, build, {"summary": "first try"})
    [again] = jobs(host, ex.resume("cus-1"))  # check failed -> back to build
    assert again["operation_id"].endswith("/build-2")
    (workspace / "result.txt").write_text("done\n")
    report(host, again, {"summary": "fixed"})
    final = ex.resume("cus-1")
    assert (final.status, final.blocker) == ("verified", "")


def test_loops_stop_at_max_visits(agent):
    workspace, make = agent
    host, ex = make()
    [job] = jobs(host, ex.start(custom_request(CHANGE)))
    report(host, job, {"summary": "plan"})
    status = ex.decide(respond(ex.resume("cus-1")))
    for attempt in (1, 2):
        [build] = jobs(host, status)
        (workspace / "result.txt").write_text(f"attempt {attempt}\n")
        report(host, build, {"summary": "tried"})
        status = ex.resume("cus-1")
    assert (status.status, status.blocker) == ("needs_review", "loop_limit")


def test_declining_ends_denied_and_write_after_check_is_not_verified(agent):
    workspace, make = agent
    host, ex = make()
    [job] = jobs(host, ex.start(custom_request(CHANGE)))
    report(host, job, {"summary": "plan"})
    denied = ex.decide(respond(ex.resume("cus-1"), choice="decline"))
    assert (denied.status, denied.blocker) == ("denied", "declined")

    tail = graph([START, {"id": "check", "type": "check"}, task("touch", "write"), END],
                 [("start", "next", "check"), ("check", "passed", "touch"),
                  ("touch", "done", "end")])
    (workspace / "result.txt").write_text("done\n")
    [touch] = jobs(host, ex.start(custom_request(tail, attempt="cus-2")))
    (workspace / "extra.txt").write_text("x")
    report(host, touch, {"summary": "changed after checks"})
    final = ex.resume("cus-2")
    assert (final.status, final.blocker) == ("needs_review", "changed_after_checks")


def test_parallel_branches_meet_at_the_join(agent):
    workspace, make = agent
    host, ex = make()
    status = ex.start(custom_request(PARALLEL, checks=[]))
    branches = jobs(host, status)
    assert sorted(j["inputs"]["step"] for j in branches) == ["a", "b"]
    host.report(branches[0]["operation_id"], "completed", {"summary": "A"})
    assert ex.resume("cus-1").status == "waiting_for_job"  # still waiting for b
    host, ex = make()
    host.report(branches[1]["operation_id"], "completed", {"summary": "B"})
    [summary] = jobs(host, ex.resume("cus-1"))
    assert summary["inputs"]["step"] == "sum"
    assert {c["summary"] for c in summary["inputs"]["context"]} == {"A", "B"}
    report(host, summary, {"summary": "both"})
    final = ex.resume("cus-1")
    assert (final.status, final.blocker) == ("needs_review", "no_checks_run")


def test_a_read_branch_that_writes_blocks_the_run(agent):
    workspace, make = agent
    host, ex = make()
    branches = jobs(host, ex.start(custom_request(PARALLEL, checks=[])))
    (workspace / "sneaky.txt").write_text("x")
    host.report(branches[0]["operation_id"], "completed", {"summary": "A"})
    host.report(branches[1]["operation_id"], "completed", {"summary": "B"})
    final = ex.resume("cus-1")
    assert (final.status, final.blocker) == ("blocked", "read_only_violation")


def test_choices_route_to_their_outcome(agent):
    workspace, make = agent
    review = graph(
        [START, task("review", choices=["approve", "changes"]), task("fix", "write"), END],
        [("start", "next", "review"), ("review", "approve", "end"),
         ("review", "changes", "fix"), ("fix", "done", "end")])
    host, ex = make()
    [job] = jobs(host, ex.start(custom_request(review)))
    assert job["inputs"]["choices"] == ["approve", "changes"]
    report(host, job, {"summary": "needs work", "choice": "changes"})
    [fix] = jobs(host, ex.resume("cus-1"))
    assert fix["inputs"]["step"] == "fix"

    [job] = jobs(host, ex.start(custom_request(review, attempt="cus-2")))
    report(host, job, {"summary": "?", "choice": "maybe"})
    final = ex.resume("cus-2")
    assert (final.status, final.blocker) == ("needs_review", "invalid_choice")
