"""Agent-driven host: jobs are handed out once and completed by reports."""

import pytest
from conftest import CHECK, change_request, research_request, respond

from langgraph_workflow import WorkflowExecutor
from langgraph_workflow.agent_host import AgentHost, ReportRejected


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


def only_job(host, status):
    assert status.status == "waiting_for_job", status
    jobs = host.pending(status.attempt_id)
    assert len(jobs) == 1
    return jobs[0]


def test_agent_drives_a_verified_change_across_restarts(agent):
    workspace, make = agent
    host, ex = make()
    status = ex.start(change_request())
    job = only_job(host, status)
    assert (job["kind"], job["access"]) == ("inspect", "read")
    assert ex.resume("att-1").status == "waiting_for_job"  # not re-dispatched
    assert len(host.pending("att-1")) == 1
    host.report(job["operation_id"], "completed", {"summary": "empty workspace"})

    host, ex = make()  # new process
    job = only_job(host, ex.resume("att-1"))
    assert job["kind"] == "plan"
    host.report(job["operation_id"], "completed",
                {"plan": {"steps": [{"title": "Write result.txt"}]}})
    waiting = ex.resume("att-1")
    assert waiting.pending_decision["kind"] == "plan_approval"  # always asks the user

    status = ex.decide(respond(waiting))
    job = only_job(host, status)
    assert (job["kind"], job["access"]) == ("write", "write")
    (workspace / "result.txt").write_text("done\n")  # the agent does the work
    receipt = host.report(job["operation_id"], "completed", {"summary": "wrote it"})
    assert receipt.changed_files == ("result.txt",)  # observed, not claimed
    final = ex.resume("att-1")
    assert final.status == "verified"
    assert all(final.result["evidence"])


def test_read_job_that_changes_files_is_blocked(agent):
    workspace, make = agent
    host, ex = make()
    job = only_job(host, ex.start(research_request()))
    (workspace / "sneaky.txt").write_text("x")
    host.report(job["operation_id"], "completed", {"claims": {}, "sources": []})
    status = ex.resume("res-1")
    assert (status.status, status.blocker) == ("blocked", "read_only_violation")


def test_claimed_success_without_changes_is_no_progress(agent):
    workspace, make = agent
    host, ex = make()
    status = ex.start(change_request(plan={"steps": [{"title": "w"}]}))
    job = only_job(host, ex.decide(respond(status)))  # supplied plans still need approval
    host.report(job["operation_id"], "completed", {"summary": "all done, tests passed"})
    repair = only_job(host, ex.resume("att-1"))  # check failed: result.txt missing
    assert repair["operation_id"].endswith("repair-1")
    host.report(repair["operation_id"], "completed", {"summary": "fixed"})
    status = ex.resume("att-1")
    assert (status.status, status.blocker) == ("needs_review", "no_progress")


def test_reports_are_validated_and_single_use(agent):
    workspace, make = agent
    host, ex = make()
    job = only_job(host, ex.start(research_request()))
    with pytest.raises(ReportRejected):
        host.report("res-1/nope", "completed", {})
    with pytest.raises(ReportRejected):
        host.report(job["operation_id"], "maybe", {})
    with pytest.raises(ReportRejected):
        host.report(job["operation_id"], "completed", {"x": object()})
    host.report(job["operation_id"], "refused", {}, "user denied web access")
    with pytest.raises(ReportRejected, match="already reported"):
        host.report(job["operation_id"], "completed", {})
    status = ex.resume("res-1")
    assert (status.status, status.blocker) == ("needs_review", "job_denied")


def test_parallel_investigations_can_be_reported_one_at_a_time(agent):
    workspace, make = agent
    host, ex = make()
    status = ex.start(research_request(investigations=["a", "b"], checks=[
        {"id": "notes", "kind": "file_exists", "path": "notes.md", "requirement": "n"}]))
    jobs = host.pending("res-1")
    assert [j["kind"] for j in jobs] == ["investigate", "investigate"]
    host.report(jobs[0]["operation_id"], "completed", {"claims": {"k": 1}, "sources": ["s0"]})
    assert ex.resume("res-1").status == "waiting_for_job"
    assert len(host.pending("res-1")) == 1
    host.report(jobs[1]["operation_id"], "completed", {"claims": {"j": 2}, "sources": ["s1"]})
    job = only_job(host, ex.resume("res-1"))
    assert job["kind"] == "synthesize"
    host.report(job["operation_id"], "completed", {"claims": {"k": 1, "j": 2}})
    status = ex.resume("res-1")
    assert status.status == "needs_review"  # notes.md missing, and research is read-only
    assert host.pending("res-1") == []


def test_cancel_closes_pending_jobs(agent):
    workspace, make = agent
    host, ex = make()
    job = only_job(host, ex.start(change_request()))
    assert ex.cancel("att-1").status == "cancelled"
    with pytest.raises(ReportRejected, match="already reported"):
        host.report(job["operation_id"], "completed", {})
    assert host.pending("att-1") == []


def test_command_checks_are_not_run_by_the_agent_host(agent):
    workspace, make = agent
    host, ex = make()
    status = ex.start(change_request(plan={"steps": [{"title": "w"}]}, checks=[
        CHECK, {"id": "tests", "kind": "command", "command": "pytest", "requirement": "t"}]))
    job = only_job(host, ex.decide(respond(status)))
    (workspace / "result.txt").write_text("done\n")
    host.report(job["operation_id"], "completed", {"summary": "ok"})
    status = ex.resume("att-1")
    assert (status.status, status.blocker) == ("needs_review", "unsupported_check")
