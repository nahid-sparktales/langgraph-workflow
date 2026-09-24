"""Process-level recovery: every restart here is a new Python interpreter."""

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import change_request, research_request, respond, run_cli, write_json

from langgraph_workflow import AttemptBusy, DecisionRejected, IncompatibleAttempt, WorkflowExecutor
from langgraph_workflow.testing import FixtureHost

DONE = {"writes": {"implement": {"result.txt": "done\n"}}}


def _setup(root, scenario):
    FixtureHost(root, scenario).save_scenario()
    return write_json(Path(root) / "request.json", change_request())


def _write_op(host):
    return next(o for o in host.operations() if o["access"] == "write")["operation_id"]


def test_interrupt_restart_and_resume_across_processes(tmp_path):
    request = _setup(tmp_path, {**DONE, "plan_approval": True})
    code, first, _ = run_cli(tmp_path, "start", request)
    assert code == 0 and first["status"] == "waiting_for_input"
    code, again, _ = run_cli(tmp_path, "status", "att-1")  # restart while waiting
    assert again["pending_decision"] == first["pending_decision"]
    pending = first["pending_decision"]
    response = write_json(tmp_path / "response.json", {
        "decision_id": pending["decision_id"], "run_id": "run-1", "attempt_id": "att-1",
        "revision": pending["revision"], "digest": pending["digest"], "choice": "approve",
        "actor": "user"})
    code, final, _ = run_cli(tmp_path, "decide", response)
    assert final["status"] == "verified"
    host = FixtureHost.from_file(tmp_path)
    assert host.effect_count() == len(host.operations()) == 3  # inspect, plan, write
    code, dup, _ = run_cli(tmp_path, "decide", response)  # duplicate delivery
    assert code == 3 and dup == {"rejected": "already_consumed"}


def test_research_interrupt_restart_resume(tmp_path):
    FixtureHost(tmp_path, {"conflict_approval": True, "findings": {
        "0": {"claims": {"k": 1}, "sources": ["s0"]},
        "1": {"claims": {"k": 2}, "sources": ["s1"]}},
        "synthesis": {"claims": {"k": 2}}}).save_scenario()
    request = write_json(tmp_path / "request.json",
                         research_request(investigations=["one", "two"]))
    code, first, _ = run_cli(tmp_path, "start", request)
    assert first["status"] == "waiting_for_input"
    pending = first["pending_decision"]
    response = write_json(tmp_path / "response.json", {
        "decision_id": pending["decision_id"], "run_id": "run-2", "attempt_id": "res-1",
        "revision": 1, "digest": pending["digest"], "choice": "prefer:1", "actor": "user"})
    code, final, _ = run_cli(tmp_path, "decide", response)
    assert (final["status"], final["blocker"]) == ("needs_review", "no_checks_declared")
    host = FixtureHost.from_file(tmp_path)
    assert host.effect_count() == 3  # two investigations + synthesis, none repeated


@pytest.mark.parametrize("point", ["before_admission", "after_admission", "after_receipt",
                                   "checkpoint"])
def test_crash_windows_resume_without_repeating_the_write(tmp_path, point):
    request = _setup(tmp_path, DONE)
    env = ({"LGW_CRASH_ON_CHECKPOINT": "after_write"} if point == "checkpoint"
           else {"LGW_FIXTURE_CRASH": f"{point}:write"})
    code, _, _ = run_cli(tmp_path, "start", request, env=env)
    assert code == 137  # really died
    code, status, err = run_cli(tmp_path, "resume", "att-1")
    assert status["status"] == "verified", err
    host = FixtureHost.from_file(tmp_path)
    assert host.effect_count(_write_op(host)) == 1


def test_crash_during_action_becomes_uncertain_and_is_never_replayed(tmp_path):
    request = _setup(tmp_path, {"writes": {"implement": {"result.txt": "done\n"},
                                           "repair": [{"result.txt": "done\n"}]}})
    code, _, _ = run_cli(tmp_path, "start", request,
                         env={"LGW_FIXTURE_CRASH": "during_action:write"})
    assert code == 137
    code, status, _ = run_cli(tmp_path, "resume", "att-1")
    assert (status["status"], status["blocker"]) == ("uncertain", "uncertain_action")
    code, again, _ = run_cli(tmp_path, "resume", "att-1")  # still blocked, still not replayed
    assert again["status"] == "uncertain"
    host = FixtureHost.from_file(tmp_path)
    op = _write_op(host)
    assert host.effect_count(op) == 1
    # A human inspects the checkout and records the observed outcome.
    host.reconcile(op, "settled", "partial write observed; repair will fix it")
    code, final, _ = run_cli(tmp_path, "resume", "att-1")
    assert final["status"] == "verified"
    assert host.effect_count(op) == 1
    assert final["repair_rounds"] == 1  # the partial write failed its check first


def test_sigkill_mid_job_then_resume(tmp_path):
    request = _setup(tmp_path, {**DONE, "job_delay": 5})
    proc = subprocess.Popen([sys.executable, "-m", "langgraph_workflow.testing",
                             str(tmp_path), "start", str(request)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    host = FixtureHost.from_file(tmp_path)
    deadline = time.time() + 30
    while not any(o["started"] for o in host.operations()) and time.time() < deadline:
        time.sleep(0.05)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    FixtureHost(tmp_path, DONE).save_scenario()
    code, status, _ = run_cli(tmp_path, "resume", "att-1")
    # The killed job was a read (inspect); its outcome was never observed.
    assert (status["status"], status["blocker"]) == ("uncertain", "uncertain_action")
    assert host.effect_count() == 1


def test_two_processes_cannot_own_one_attempt(tmp_path):
    request = _setup(tmp_path, {**DONE, "job_delay": 1.5})
    proc = subprocess.Popen([sys.executable, "-m", "langgraph_workflow.testing",
                             str(tmp_path), "start", str(request)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    host = FixtureHost.from_file(tmp_path)
    while not host.operations():
        time.sleep(0.05)
    code, _, err = run_cli(tmp_path, "resume", "att-1")
    assert code != 0 and "AttemptBusy" in err
    out, _ = proc.communicate(timeout=60)
    assert '"verified"' in out
    assert host.effect_count() == len(host.operations())


def test_cancel_writer_reaches_quiescence(make, tmp_path):
    host, ex = make({"plan_approval": False, "job_delay": 0.4,
                     "writes": {"implement": {"result.txt": "done\n"}}})
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault(
        "s", ex.start(change_request(plan={"steps": [{"title": "w"}]}))))
    worker.start()
    while not any(o["started"] for o in host.operations()):
        time.sleep(0.01)
    other = WorkflowExecutor(FixtureHost(tmp_path, host.scenario), tmp_path / "checkpoints.sqlite3")
    other.cancel("att-1")  # like a second process
    after_cancel = (host.workspace / "result.txt").exists()
    worker.join(10)
    assert result["s"].status == "cancelled", result["s"]
    time.sleep(0.5)
    assert (host.workspace / "result.txt").exists() == after_cancel is False
    assert other.status("att-1").status == "cancelled"
    other.close()


def test_cancel_pending_approval(make):
    host, ex = make({"plan_approval": True})
    waiting = ex.start(change_request())
    assert ex.cancel("att-1").status == "cancelled"
    with pytest.raises(DecisionRejected):
        ex.decide(respond(waiting))
    assert not any(o["access"] == "write" for o in host.operations())


def test_cancel_queued_parallel_jobs(make):
    host, ex = make({"job_delay": 0.3, "findings": {}})
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault(
        "s", ex.start(research_request(investigations=["a", "b", "c"]))))
    worker.start()
    while host.active_reads < 2:
        time.sleep(0.01)
    ex.store.request("res-1", "cancel")
    host.cancel("res-1")
    worker.join(10)
    assert result["s"].status == "cancelled"
    assert host.effect_count() <= 2  # the queued third investigation never started


def test_concurrent_writers_on_one_workspace_are_serialized(make, tmp_path):
    host, ex = make({"job_delay": 0.5, "writes": {"implement": {"result.txt": "done\n"}}})
    worker = threading.Thread(target=lambda: ex.start(change_request(
        plan={"steps": [{"title": "w"}]})))
    worker.start()
    while not any(o["started"] for o in host.operations()):
        time.sleep(0.01)
    host2 = FixtureHost(tmp_path, {**host.scenario, "job_delay": 0})
    ex2 = WorkflowExecutor(host2, tmp_path / "checkpoints.sqlite3")
    second = ex2.start(change_request("att-2", plan={"steps": [{"title": "w2"}]}))
    assert (second.status, second.blocker) == ("waiting_for_capability", "workspace_busy")
    worker.join(10)
    assert ex2.resume("att-2").status == "verified"
    ex2.close()


def test_sqlite_busy_is_waited_out(make, tmp_path):
    host, ex = make(DONE)
    ex.store.open()
    blocker = sqlite3.connect(tmp_path / "checkpoints.sqlite3", timeout=5,
                              check_same_thread=False)
    blocker.execute("BEGIN EXCLUSIVE")
    threading.Timer(0.5, blocker.rollback).start()
    assert ex.start(change_request()).status == "verified"
    blocker.close()


def test_incompatible_saved_definition_is_an_actionable_blocker(make, tmp_path):
    host, ex = make({"plan_approval": True})
    ex.start(change_request())
    with sqlite3.connect(tmp_path / "checkpoints.sqlite3") as db:
        db.execute("UPDATE lgw_attempts SET definition_version='verified_change/0'")
    with pytest.raises(IncompatibleAttempt, match="start a new attempt"):
        ex.resume("att-1")
    assert len(host.operations()) == 2  # nothing restarted from scratch


def test_attempts_do_not_share_state(make):
    host, ex = make({**DONE, "plan_approval": True})
    a = ex.start(change_request("att-a", run_id="run-a"))
    b = ex.start(change_request("att-b", run_id="run-b"))
    assert set(a.jobs) & set(b.jobs) == set()
    with pytest.raises(DecisionRejected) as rejected:
        ex.decide({**respond(a), "attempt_id": "att-b", "run_id": "run-a"})
    assert rejected.value.code == "cross_run"


def test_retention_prunes_only_terminal_attempts(make):
    host, ex = make({**DONE, "plan_approval": True})
    ex.start(change_request("att-done", plan={"steps": [{"title": "w"}]}))
    ex.decide(respond(ex.status("att-done")))
    waiting = ex.start(change_request("att-wait"))
    pruned = ex.store.prune(older_than_seconds=0, terminal=("verified",))
    assert pruned == ["att-done"]
    assert ex.status("att-wait").pending_decision == waiting.pending_decision


def test_lease_blocks_second_owner_in_process(make, tmp_path):
    host, ex = make(DONE)
    ex.store.open()
    other = WorkflowExecutor(host, tmp_path / "checkpoints.sqlite3")
    with ex.store.lease("att-x"):
        with pytest.raises(AttemptBusy):
            with other.store.lease("att-x"):
                pass
    other.close()
