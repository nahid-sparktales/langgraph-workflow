"""Contracts, reducers, limits, import inertness, and the thread/event-loop bridge."""

import asyncio
import itertools
import json
import subprocess
import sys
import threading
import time

import pytest
from conftest import change_request

from langgraph_workflow import ContractError, WorkflowExecutor, WorkflowRequest
from langgraph_workflow.contracts import JobSpec
from langgraph_workflow.policy import effective_limits
from langgraph_workflow.state import merge_receipts


def _r(op, status, job="j"):
    return {op: {"operation_id": op, "status": status, "job_id": job}}


def test_fan_in_reducer_is_order_independent_and_idempotent():
    updates = [_r("a", "uncertain"), _r("a", "settled", "j1"), _r("b", "running"),
               _r("b", "settled"), _r("a", "settled", "j0")]
    results = set()
    for order in itertools.permutations(updates):
        merged = {}
        for update in order:
            merged = merge_receipts(merged, update)
            merged = merge_receipts(merged, update)  # replayed branch
        results.add(json.dumps(merged, sort_keys=True))
    assert len(results) == 1
    merged = json.loads(results.pop())
    assert list(merged) == ["a", "b"]
    assert merged["a"]["status"] == merged["b"]["status"] == "settled"


def test_limits_only_narrow():
    limits = effective_limits({"max_jobs": 50, "max_repair_rounds": 1}, {"max_writers": 3},
                              {"max_parallel_reads": 0, "unknown": 9, "max_jobs": "x"})
    assert limits["max_jobs"] == 12 and limits["max_repair_rounds"] == 1
    assert limits["max_writers"] == 1 and limits["max_parallel_reads"] == 1
    assert "unknown" not in limits


@pytest.mark.parametrize("patch", [
    {"workflow": "arbitrary_graph"},
    {"attempt_id": "../../etc"},
    {"attempt_id": "a" * 500},
    {"goal": ""},
    {"goal": "x" * 20_000},
    {"checks": [{"kind": "file_exists"}]},
    {"plan": {"steps": []}},
    {"limits": {"max_jobs": -1}},
    {"reviewer": "yes"},
    {"__import__": "os"},
])
def test_request_validation_rejects(patch):
    with pytest.raises(ContractError):
        WorkflowRequest.from_dict({**change_request(), **patch})


def test_job_spec_rejects_unknown_kind_and_non_json():
    with pytest.raises(ContractError):
        JobSpec("a/b", "r", "a", "shell", "write", "x")
    with pytest.raises(ContractError):
        JobSpec("a/b", "r", "a", "write", "write", "x", inputs={"f": object()})


def test_import_and_construction_are_inert(tmp_path):
    code = f"""
import threading, sys, os
before = set(threading.enumerate())
import langgraph_workflow
from langgraph_workflow import WorkflowExecutor
ex = WorkflowExecutor(object(), {str(tmp_path / 'never' / 'db.sqlite3')!r})
assert set(threading.enumerate()) == before, threading.enumerate()
assert not os.path.exists({str(tmp_path / 'never')!r})
import logging
assert logging.getLogger().handlers == []
print("inert")
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "inert", out.stderr


def test_blocking_call_on_event_loop_is_refused_and_bridge_works(make):
    host, ex = make({"writes": {"implement": {"result.txt": "done\n"}}, "job_delay": 0.05})

    async def main():
        with pytest.raises(RuntimeError, match="event-loop"):
            ex.start(change_request())
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)

        beat = asyncio.create_task(heartbeat())
        status = await ex.astart(change_request())
        beat.cancel()
        return status, ticks

    status, ticks = asyncio.run(main())
    assert status.status == "verified"
    assert ticks > 10  # the loop kept running while the graph executed


def test_cooperative_pause_from_another_thread(make):
    host, ex = make({"job_delay": 0.3, "writes": {"implement": {"result.txt": "done\n"}}})
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("s", ex.start(change_request())))
    worker.start()
    time.sleep(0.1)
    ex.pause("att-1")
    worker.join(10)
    paused = result["s"]
    assert (paused.status, paused.blocker) == ("paused", "paused")
    done_before = len(host.operations())
    assert done_before < 3  # stopped at a super-step boundary, not at the end
    final = ex.resume("att-1")
    assert final.status == "verified"
    assert host.effect_count() == len(host.operations())  # no job ran twice


def test_executor_status_for_unknown_attempt(make):
    host, ex = make()
    with pytest.raises(KeyError):
        ex.status("missing")
    assert isinstance(ex, WorkflowExecutor)


def test_one_executor_never_drives_an_attempt_twice(make):
    from langgraph_workflow import AttemptBusy
    host, ex = make({"job_delay": 0.5, "writes": {"implement": {"result.txt": "done\n"}}})
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("s", ex.start(change_request())))
    worker.start()
    while not host.operations():
        time.sleep(0.01)
    with pytest.raises(AttemptBusy):
        ex.resume("att-1")  # same instance, second thread
    # Only flags + host.cancel here; the driving thread routes to finish. The
    # host waits for quiescence, so the driver may already be done.
    assert ex.cancel("att-1").status in ("running", "cancel_requested", "cancelled")
    worker.join(10)
    assert result["s"].status == "cancelled"
    assert host.effect_count() == len(host.operations())
