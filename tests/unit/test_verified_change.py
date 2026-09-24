"""Branch coverage for the verified-change graph on the fixture host."""

import pytest
from conftest import CHECK, change_request, respond

from langgraph_workflow import DecisionRejected


def test_happy_path_with_repair_and_review(make):
    host, ex = make({"review": ["changes_requested", "approve"],
                     "writes": {"implement": {"result.txt": "draft\n"},
                                "repair": [{"result.txt": "done\n"},
                                           {"result.txt": "done, reviewed\n"}]}})
    status = ex.start(change_request(reviewer=True))
    assert status.status == "verified", status
    assert status.repair_rounds == 2
    kinds = [op["operation_id"].split("/")[1] for op in host.operations()]
    # review requested changes -> repair -> re-verify -> re-review -> final verify
    assert kinds[-3:] == ["review-1", "repair-2", "review-2"]
    assert status.result["evidence"] and all(status.result["evidence"])
    assert (host.workspace / "result.txt").read_text() == "done, reviewed\n"


def test_supplied_plan_skips_inspect_and_plan_jobs(make):
    host, ex = make({"writes": {"implement": {"result.txt": "done\n"}}})
    status = ex.start(change_request(plan={"steps": [{"title": "write it"}]}))
    assert status.status == "verified"
    assert [op["kind"] for op in host.operations()] == ["write"]


def test_configured_reviewer_is_not_skipped_for_trivial_change(make):
    host, ex = make({"writes": {"implement": {"result.txt": "done\n"}}})
    status = ex.start(change_request(plan={"steps": [{"title": "t"}]}, reviewer=True))
    assert status.status == "verified"
    assert [op["kind"] for op in host.operations()] == ["write", "review"]


def test_missing_capability_waits_and_runs_no_job(make):
    host, ex = make({"capabilities": ["jobs.read", "events", "cancel"]})
    assert ex.validate(change_request()) == ["missing_capability: jobs.write",
                                             "missing_capability: verify"]
    status = ex.start(change_request())
    assert (status.status, status.blocker) == ("waiting_for_capability", "missing_capability")
    assert host.operations() == []


def test_not_admitted_blocks(make):
    host, ex = make({"admit": False, "admit_reason": "workspace_locked_by_policy"})
    status = ex.start(change_request())
    assert (status.status, status.blocker) == ("blocked", "workspace_locked_by_policy")
    assert host.operations() == []


def test_plan_denied(make):
    host, ex = make({"plan_approval": True})
    status = ex.start(change_request())
    assert status.status == "waiting_for_input"
    assert status.pending_decision["kind"] == "plan_approval"
    status = ex.decide(respond(status, "deny"))
    assert (status.status, status.blocker) == ("denied", "plan_denied")
    assert not any(op["access"] == "write" for op in host.operations())


def test_edited_plan_needs_fresh_approval(make):
    host, ex = make({"plan_approval": True})
    first = ex.start(change_request())
    edited = {"steps": [{"title": "Write result.txt"}, {"title": "Double-check"}]}
    second = ex.decide(respond(first, "edit", edited_plan=edited))
    assert second.status == "waiting_for_input"
    assert second.pending_decision["revision"] == 2
    assert second.pending_decision["digest"] != first.pending_decision["digest"]
    # The old approval cannot be replayed against the new revision.
    with pytest.raises(DecisionRejected) as rejected:
        ex.decide(respond(first, "approve"))
    assert rejected.value.code in ("stale_decision", "already_consumed")
    final = ex.decide(respond(second, "approve"))
    assert final.status == "verified"


def test_decision_rejections(make):
    host, ex = make({"plan_approval": True})
    status = ex.start(change_request())
    good = respond(status)
    for bad, code in (
        ({**good, "run_id": "run-other"}, "cross_run"),
        ({**good, "attempt_id": "nope"}, "unknown_attempt"),
        ({**good, "revision": 2}, "stale_revision"),
        ({**good, "digest": "0" * 64}, "stale_revision"),
        ({**good, "decision_id": "att-1:plan:9"}, "stale_decision"),
        ({**good, "choice": "yolo"}, "invalid_choice"),
        ({**good, "actor": "stranger"}, "unauthorized"),
        ({**good, "choice": "edit"}, "edit_without_plan"),
    ):
        with pytest.raises(DecisionRejected) as rejected:
            ex.decide(bad)
        assert rejected.value.code == code
    assert ex.decide(good).status == "verified"
    with pytest.raises(DecisionRejected) as rejected:
        ex.decide(good)  # duplicate delivery after it was applied
    assert rejected.value.code == "already_consumed"


def test_revoked_policy_rejects_decision_and_resume(make):
    host, ex = make({"plan_approval": True})
    status = ex.start(change_request())
    host.revoke("att-1")
    with pytest.raises(DecisionRejected) as rejected:
        ex.decide(respond(status))
    assert rejected.value.code == "revoked"


def test_repairs_exhausted_is_needs_review(make):
    host, ex = make({"writes": {"implement": {"result.txt": "a\n"},
                                "repair": [{"result.txt": "b\n"}, {"result.txt": "c\n"}]}})
    status = ex.start(change_request())
    assert (status.status, status.blocker) == ("needs_review", "repairs_exhausted")
    assert status.repair_rounds == 2


def test_repair_without_changes_is_no_progress(make):
    host, ex = make({"writes": {"implement": {"result.txt": "a\n"}, "repair": [{}]}})
    status = ex.start(change_request())
    assert (status.status, status.blocker) == ("needs_review", "no_progress")


def test_forged_verification_cannot_verify(make):
    host, ex = make({"forge_verification": True,
                     "writes": {"implement": {"result.txt": "done\n"}}})
    status = ex.start(change_request())
    assert (status.status, status.blocker) == ("needs_review", "missing_evidence")


def test_job_prose_is_not_evidence(make):
    # The writer "claims" success but the declared check fails.
    host, ex = make({"writes": {"implement": {"result.txt": "tests passed!\n"}, "repair": []},
                     "limits": {"max_repair_rounds": 0}})
    status = ex.start(change_request())
    assert status.status == "needs_review"


@pytest.mark.parametrize("check,blocker", [
    ({"id": "h", "kind": "human_review", "requirement": "owner signs off"},
     "human_review_required"),
    ({"id": "c", "kind": "command", "command": "pytest", "requirement": "tests"},
     "unsupported_check"),
    ({"id": "t", "kind": "file_exists", "path": "../outside", "requirement": "x"},
     "check_denied"),
])
def test_unverifiable_checks_need_review(make, check, blocker):
    host, ex = make({"writes": {"implement": {"result.txt": "done\n"}}})
    status = ex.start(change_request(checks=[CHECK, check]))
    assert (status.status, status.blocker) == ("needs_review", blocker)


def test_no_declared_checks_never_verifies(make):
    host, ex = make({"writes": {"implement": {"result.txt": "done\n"}}})
    status = ex.start(change_request(checks=[]))
    assert (status.status, status.blocker) == ("needs_review", "no_checks_declared")


def test_job_denied_by_host(make):
    host, ex = make({"fail": {"write": "denied"}})
    status = ex.start(change_request())
    assert (status.status, status.blocker) == ("needs_review", "job_denied")


def test_host_budget_exhaustion_parks_then_resumes_when_host_allows(make):
    host, ex = make({"fail": {"write": "budget_exhausted"}})
    status = ex.start(change_request())
    assert (status.status, status.blocker) == ("budget_exhausted", "budget_exhausted")
    assert ex.resume("att-1").status == "budget_exhausted"
    assert host.effect_count() == 2  # inspect + plan; the refused write never ran
    # The host raises its allowance (an explicit host-side change).
    host.scenario["fail"] = {}
    host.scenario["writes"] = {"implement": {"result.txt": "done\n"}}
    assert ex.resume("att-1").status == "verified"
    assert host.effect_count() == 3


def test_graph_job_limit_survives_resume(make):
    host, ex = make({"limits": {"max_jobs": 3}})
    status = ex.start(change_request())  # inspect, plan, implement; repair needs a 4th
    assert (status.status, status.blocker) == ("budget_exhausted", "budget_exhausted")
    assert ex.resume("att-1").status == "budget_exhausted"  # allowance is not restored
    assert len(host.operations()) == 3


def test_failed_writer_is_failed(make):
    host, ex = make({"fail": {"write": "failed"}})
    status = ex.start(change_request())
    assert (status.status, status.blocker) == ("failed", "implement_failed")


def test_invalid_plan_from_planner(make):
    host, ex = make({"plan": {"steps": []}})
    status = ex.start(change_request())
    assert (status.status, status.blocker) == ("needs_review", "invalid_plan")


def test_duplicate_start_resumes_instead_of_forking(make):
    host, ex = make({"writes": {"implement": {"result.txt": "done\n"}}})
    first = ex.start(change_request())
    second = ex.start(change_request())
    assert first.status == second.status == "verified"
    assert host.effect_count() == len(host.operations())
    with pytest.raises(ValueError):
        ex.start(change_request(goal="something else"))


def test_transition_limit_from_host_admission_bounds_the_graph(make):
    host, ex = make({"review": ["changes_requested"] * 10,
                     "writes": {"implement": {"result.txt": "done\n"},
                                "repair": [{"result.txt": f"done {i}\n"} for i in range(10)]},
                     "limits": {"max_transitions": 10}})
    status = ex.start(change_request(reviewer=True))
    assert (status.status, status.blocker) == ("failed", "transition_limit")


def test_unknown_usage_stays_unknown_and_is_not_authoritative(make):
    host, ex = make({"writes": {"implement": {"result.txt": "done\n"}},
                     "usage": {"coverage": "unknown"}})
    status = ex.start(change_request(plan={"steps": [{"title": "w"}]}))
    assert status.status == "verified"
    assert status.usage == {"calls": 1, "coverage": "unknown", "authoritative": False}
