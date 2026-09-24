"""Headless integration against real Locus code.

Requires a disposable checkout prepared by integrations/locus/prepare_checkout.sh
and ``LOCUS_PYTHON`` pointing at its venv interpreter. Skipped otherwise: the
fixture-host suites establish package behavior, not Locus compatibility.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

PYTHON = os.environ.get("LOCUS_PYTHON")
HARNESS = Path(__file__).parents[2] / "integrations" / "locus" / "harness.py"
pytestmark = [pytest.mark.locus, pytest.mark.skipif(
    not PYTHON, reason="LOCUS_PYTHON not set (see integrations/locus)")]


def harness(home, scenario, *extra):
    done = subprocess.run([PYTHON, str(HARNESS), str(home), scenario, *extra],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_verified_change_restart_between_approval_and_execution(tmp_path):
    first = harness(tmp_path, "change-start")
    assert first["status"]["status"] == "waiting_for_input"
    assert first["reader_calls"] == ["inspect", "plan"]
    assert {a["state"] for a in first["attempts"]} == {"completed"}
    assert {e["state"] for e in first["usage_summary"]["entries"]} == {"settled"}

    second = harness(tmp_path, "change-decide")  # new process, same temp profile
    status = second["status"]
    assert status["status"] == "verified"
    assert second["reader_calls"] == []  # settled read jobs were not repeated
    assert second["writer_calls"] == 2
    assert second["permission_decisions"] == [["write_file", "once"]]
    assert "permission_request" in second["core_events"]
    assert second["task_completion"][0] == "passed"  # Locus's own verdict agrees
    assert all(status["result"]["evidence"])
    assert second["result_file"] == "done\n"
    assert second["forged_actor"] == "unauthorized"
    assert second["workflow_event_ids_unique"]
    assert second["events_after_duplicate_publish"] == 0
    assert second["replay_from_cursor"] == [s for s, _ in second["workflow_events"][-2:]]
    assert len(second["attempts"]) == 3


def test_file_changed_after_verification_is_not_verified(tmp_path):
    report = harness(tmp_path, "stale-final")
    status = report["status"]
    assert (status["status"], status["blocker"]) == ("needs_review", "final_checks_failed")
    assert report["result_file"] == "tampered\n"
    assert report["task_completion"][0] == "failed"


def test_denied_write_is_honest(tmp_path):
    report = harness(tmp_path, "change-deny-writes")
    assert (report["status"]["status"], report["status"]["blocker"]) == ("needs_review",
                                                                         "job_denied")
    assert report["result_file"] is None
    assert report["permission_decisions"] == [["write_file", "deny"]]


def test_parallel_read_only_research(tmp_path):
    report = harness(tmp_path, "research")
    assert report["status"]["status"] == "verified"
    assert sorted(report["reader_calls"]) == ["investigate", "investigate", "synthesize"]
    assert len(report["usage_summary"]["entries"]) == 3
    assert report["writer_calls"] == 0


def test_without_extraction_patch_read_jobs_wait_for_capability(tmp_path):
    report = harness(tmp_path, "unpatched")
    blocked = report["without_supplied_plan"]
    assert (blocked["status"], blocked["detail"]) == ("waiting_for_capability", "jobs.read")
    assert report["status"]["status"] == "verified"  # supplied plan needs no read job


def test_locus_adapter_satisfies_host_contract(tmp_path):
    results = harness(tmp_path, "contract")["contract"]
    assert all(v == "pass" for v in results.values()), results
    assert len(results) == 17


def test_plugin_installs_from_marketplace_and_runs_in_locus(tmp_path):
    # Local folder by default; LGW_PLUGIN_SOURCE=owner/repo tests the GitHub download.
    source = os.environ.get("LGW_PLUGIN_SOURCE", str(Path(__file__).parents[2]))
    report = harness(tmp_path, "plugin", source)
    assert report["marketplace"]["error"] is None
    assert report["installed"]["digest"] == report["trust"]["digest"]
    assert report["trust"]["unsupported"] == []
    assert report["server_state"] == "connected", report["server_error"]
    # workflow_decide is never an agent tool: released Locus does not start the
    # server with it, and a panel-aware Locus hides it from agents.
    assert report["tools"] == ["workflow_cancel", "workflow_definition", "workflow_definitions",
                               "workflow_overview", "workflow_report", "workflow_run",
                               "workflow_start", "workflow_status"]
    assert report["prompts"][0] == "Approve this plan?\n\nCreate result.txt"  # Locus's prompt
    assert [s[2] for s in report["statuses"]] == [["inspect"], ["plan"], ["write"], []]
    assert report["final"]["status"] == "verified"
    assert report["result_file"] == "done\n"
    window = report["window"]
    if window is None:
        assert len(report["prompts"]) == 1
    else:  # a Locus build with plugin panels: declined in chat, approved in the window
        assert report["prompts"][1:] == ["Approve this plan?\n\nTouch result.txt"]
        [panel] = window["panels"]
        assert panel["id"] == "workflows" and "workflow_dispatch" in panel["tools"]
        assert {"agents.read", "agents.dispatch"} <= set(panel["capabilities"])
        assert window["saved_definition"] == "two-agents"
        assert window["launched"] == ["waiting_for_job", "Nova"]
        assert "Plan the change" in window["handoff_text"] and window["stolen"].startswith("Error")
        assert window["default_values"]["reviewer_default"] is False
        assert window["saved_values"]["reviewer_default"] is True
        assert window["overview_settings"]["reviewer_default"] is True
        assert window["run_reviewer"] is True
        assert window["decided"] == ["waiting_for_job", ["write"]]
