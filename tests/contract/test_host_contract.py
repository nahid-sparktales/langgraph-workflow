"""The shared host contract, run against the fixture host.

The Locus reference adapter runs the same ``run_host_contract`` through
integrations/locus/harness.py (see tests/integration/test_locus.py).
"""

import sqlite3

from langgraph_workflow import WorkflowRequest
from langgraph_workflow.testing import FixtureHost, run_host_contract


def test_fixture_host_satisfies_contract(tmp_path):
    host = FixtureHost(tmp_path)
    request = WorkflowRequest.from_dict({
        "workflow": "verified_change", "run_id": "run-c", "task_id": "task-c",
        "attempt_id": "contract-1", "workspace_id": "ws", "goal": "contract"})

    def count(event_id):
        with sqlite3.connect(host.db_path) as db:
            return db.execute("SELECT COUNT(*) FROM events WHERE event_id=?",
                              (event_id,)).fetchone()[0]

    results = run_host_contract(host, request, count_events=count)
    assert all(v == "pass" for v in results.values()), results
    assert len(results) == 17
