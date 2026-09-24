"""Research workflow: fan-out/fan-in, provenance, conflicts, isolation, limits."""

import pytest
from conftest import research_request, respond

from langgraph_workflow import DecisionRejected

SOURCES = {"0": ["doc://a"], "1": ["doc://b"], "2": ["doc://c"]}
CHECK = {"id": "deliverable", "kind": "file_exists", "path": "notes.md", "requirement": "notes"}


def findings(**claims_by_index):
    return {i: {"claims": claims, "sources": SOURCES[i]} for i, claims in claims_by_index.items()}


def test_single_investigation_with_host_check(make):
    host, ex = make({"findings": findings(**{"0": {"engine": "sqlite"}}),
                     "synthesis": {"summary": "use sqlite", "claims": {"engine": "sqlite"},
                                   "sources": ["doc://a"]}})
    (host.workspace / "notes.md").write_text("notes")
    status = ex.start(research_request(checks=[CHECK]))
    assert status.status == "verified", status
    assert [o["kind"] for o in host.operations()] == ["investigate", "synthesize"]


def test_without_checks_result_needs_review(make):
    host, ex = make({"findings": findings(**{"0": {"engine": "sqlite"}}),
                     "synthesis": {"claims": {"engine": "sqlite"}}})
    status = ex.start(research_request())
    assert (status.status, status.blocker) == ("needs_review", "no_checks_declared")


def test_parallel_fan_out_is_bounded_and_deterministic(make):
    host, ex = make({"findings": findings(**{"0": {"a": 1}, "1": {"b": 2}, "2": {"c": 3}}),
                     "job_delay": 0.05,
                     "synthesis": {"claims": {"a": 1, "b": 2, "c": 3}}})
    (host.workspace / "notes.md").write_text("notes")
    status = ex.start(research_request(investigations=["q one", "q two", "q three"],
                                       checks=[CHECK]))
    assert status.status == "verified"
    assert host.max_active_reads == 2  # default max_parallel_reads
    values, _ = ex._snapshot("res-1")
    assert [f["index"] for f in sorted(values["findings"].values(),
                                       key=lambda f: f["operation_id"])] == [0, 1, 2]
    assert values["routing"]["mode"] == "parallel"


def test_duplicate_questions_are_not_investigated_twice(make):
    host, ex = make({"findings": findings(**{"0": {"a": 1}}), "synthesis": {"claims": {"a": 1}}})
    ex.start(research_request(investigations=["Same Q", "same   q"]))
    assert [o["kind"] for o in host.operations()] == ["investigate", "synthesize"]
    values, _ = ex._snapshot("res-1")
    assert values["routing"]["duplicates_dropped"] == ["same   q"]


def test_scope_over_limit_is_blocked_before_any_job(make):
    host, ex = make()
    status = ex.start(research_request(investigations=[f"q{i}" for i in range(5)]))
    assert (status.status, status.blocker) == ("blocked", "scope_exceeds_limit")
    assert host.operations() == []


def test_fan_out_reserves_job_budget_up_front(make):
    host, ex = make({"limits": {"max_jobs": 2}})
    status = ex.start(research_request(investigations=["a", "b"]))
    assert (status.status, status.blocker) == ("budget_exhausted", "budget_exhausted")
    assert host.operations() == []


def test_conflicts_are_reported_when_not_asked(make):
    host, ex = make({"findings": findings(**{"0": {"engine": "sqlite"}, "1": {"engine": "pg"}}),
                     "synthesis": {"claims": {}, "conflicts_reported": []}})
    status = ex.start(research_request(investigations=["a", "b"]))
    assert (status.status, status.blocker) == ("needs_review", "conflicts_unreported")
    values, _ = ex._snapshot("res-1")
    assert values["conflicts"] == [{"key": "engine", "values": {"0": "sqlite", "1": "pg"}}]


def test_conflict_decision_interrupts_and_resumes(make):
    host, ex = make({"conflict_approval": True,
                     "findings": findings(**{"0": {"engine": "sqlite"}, "1": {"engine": "pg"}}),
                     "synthesis": {"claims": {"engine": "pg"}}})
    (host.workspace / "notes.md").write_text("notes")
    status = ex.start(research_request(investigations=["a", "b"], checks=[CHECK]))
    assert status.status == "waiting_for_input"
    assert status.pending_decision["kind"] == "conflict_resolution"
    assert "prefer:1" in status.pending_decision["options"]
    status = ex.decide(respond(status, "prefer:1"))
    assert status.status == "verified"


def test_unsupported_synthesized_claim_needs_review(make):
    host, ex = make({"findings": findings(**{"0": {"engine": "sqlite"}}),
                     "synthesis": {"claims": {"engine": "made up"}}})
    (host.workspace / "notes.md").write_text("notes")
    status = ex.start(research_request(checks=[CHECK]))
    assert (status.status, status.blocker) == ("needs_review", "unsupported_claim")


def test_unsourced_findings_cannot_support_claims(make):
    host, ex = make({"findings": {"0": {"claims": {"engine": "sqlite"}, "sources": []}},
                     "synthesis": {"claims": {"engine": "sqlite"}}})
    (host.workspace / "notes.md").write_text("notes")
    status = ex.start(research_request(checks=[CHECK]))
    assert (status.status, status.blocker) == ("needs_review", "unsupported_claim")


def test_read_only_job_that_writes_is_blocked(make):
    host, ex = make()
    real = host._perform

    def misbehaving(spec, refusal):
        status, output, _ = real(spec, refusal)
        return status, output, ["sneaky.txt"]

    host._perform = misbehaving
    status = ex.start(research_request())
    assert (status.status, status.blocker) == ("blocked", "read_only_violation")


def test_denied_investigation_stops_honestly(make):
    host, ex = make({"fail": {"investigate": "denied"}})
    status = ex.start(research_request(investigations=["a", "b"]))
    assert (status.status, status.blocker) == ("needs_review", "job_denied")


def test_stale_conflict_decision_rejected(make):
    host, ex = make({"conflict_approval": True,
                     "findings": findings(**{"0": {"x": 1}, "1": {"x": 2}})})
    status = ex.start(research_request(investigations=["a", "b"]))
    with pytest.raises(DecisionRejected) as rejected:
        ex.decide({**respond(status, "report"), "digest": "f" * 64})
    assert rejected.value.code == "stale_revision"
