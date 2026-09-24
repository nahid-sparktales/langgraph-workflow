"""Offline demo: real LangGraph graphs, a real SQLite checkpoint, real process
restarts. The host and its "model" outputs are the deterministic fixture host
(labelled as fixture data in every receipt); nothing touches a network, an
account, or a Locus profile.

    python examples/demo.py [--profile DIR]

Without --profile a fresh temporary directory is used and kept for inspection.
Every step below runs in a new Python process.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from langgraph_workflow.testing import FixtureHost


def step(root: Path, *args: str, env: dict | None = None) -> dict | None:
    done = subprocess.run([sys.executable, "-m", "langgraph_workflow.testing", str(root), *args],
                          capture_output=True, text=True, env={**os.environ, **(env or {})})
    if done.returncode == 137:
        print("    process killed (simulated crash)")
        return None
    if done.returncode not in (0, 3):
        raise SystemExit(done.stderr)
    return json.loads(done.stdout.strip().splitlines()[-1])


def show(label: str, status: dict | None) -> None:
    if status is None:
        return
    if "rejected" in status:
        print(f"    {label}: rejected ({status['rejected']})")
        return
    extra = f" blocker={status['blocker']}" if status.get("blocker") else ""
    print(f"    {label}: {status['status']}{extra}  jobs={sorted(status['jobs'].values())}")


def answer(root: Path, status: dict, choice: str) -> Path:
    pending = status["pending_decision"]
    path = root / f"decision-{pending['revision']}-{choice.replace(':', '-')}.json"
    path.write_text(json.dumps({
        "decision_id": pending["decision_id"], "run_id": status["run_id"],
        "attempt_id": status["attempt_id"], "revision": pending["revision"],
        "digest": pending["digest"], "choice": choice, "actor": "user"}))
    return path


def verified_change(root: Path) -> None:
    print("\n1. Verified change with plan approval, restart, and a crash mid-checkpoint")
    FixtureHost(root, {
        "plan_approval": True, "review": ["changes_requested", "approve"],
        "writes": {"implement": {"result.txt": "draft\n"},
                   "repair": [{"result.txt": "done\n"}, {"result.txt": "done, reviewed\n"}]},
    }).save_scenario()
    request = root / "request.json"
    request.write_text(json.dumps({
        "workflow": "verified_change", "run_id": "demo-run", "task_id": "demo-task",
        "attempt_id": "demo-change-1", "workspace_id": "demo-ws", "reviewer": True,
        "goal": "Make result.txt say done",
        "checks": [{"id": "result", "kind": "file_contains", "path": "result.txt",
                    "value": "done", "requirement": "result.txt says done"}]}))
    waiting = step(root, "start", str(request))
    show("process 1 start", waiting)
    print(f"    pending decision: {waiting['pending_decision']['summary']!r}")
    show("process 2 status after restart", step(root, "status", "demo-change-1"))
    decision = answer(root, waiting, "approve")
    step(root, "decide", str(decision), env={"LGW_CRASH_ON_CHECKPOINT": "after_write"})
    show("process 3 decide (killed inside a checkpoint write)", None)
    show("process 4 status", step(root, "status", "demo-change-1"))
    final = step(root, "resume", "demo-change-1")
    show("process 5 resume", final)
    show("process 6 duplicate decision", step(root, "decide", str(decision)))
    host = FixtureHost.from_file(root)
    writes = [o for o in host.operations() if o["access"] == "write"]
    print(f"    writer operations: {len(writes)}, executions: "
          f"{sum(host.effect_count(o['operation_id']) for o in writes)} (never repeated)")
    print(f"    evidence receipts: {final['result']['evidence']}")
    print(f"    result.txt: {(host.workspace / 'result.txt').read_text()!r}")


def research(root: Path) -> None:
    print("\n2. Read-only research: bounded fan-out, conflict decision across a restart")
    FixtureHost(root, {
        "conflict_approval": True, "job_delay": 0.05,
        "findings": {"0": {"claims": {"engine": "sqlite"}, "sources": ["doc://a"]},
                     "1": {"claims": {"engine": "postgres"}, "sources": ["doc://b"]},
                     "2": {"claims": {"wal": True}, "sources": ["doc://c"]}},
        "synthesis": {"summary": "fixture synthesis", "claims": {"engine": "sqlite", "wal": True}},
    }).save_scenario()
    (root / "workspace").mkdir(exist_ok=True)
    (root / "workspace" / "notes.md").write_text("notes\n")
    request = root / "request.json"
    request.write_text(json.dumps({
        "workflow": "research", "run_id": "demo-run", "task_id": "demo-task",
        "attempt_id": "demo-research-1", "workspace_id": "demo-ws",
        "goal": "Pick a cache storage engine",
        "investigations": ["Which engine fits?", "What do operators prefer?", "Journal mode?"],
        "checks": [{"id": "notes", "kind": "file_exists", "path": "notes.md",
                    "requirement": "notes delivered"}]}))
    waiting = step(root, "start", str(request))
    show("process 1 start", waiting)
    print(f"    pending decision: {waiting['pending_decision']['summary']!r}")
    final = step(root, "decide", str(answer(root, waiting, "prefer:0")))
    show("process 2 decide prefer:0", final)
    events = step(root, "events", "0")
    print(f"    {len(events)} durable events; types: "
          f"{sorted({e['type'] for e in events})}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", type=Path, help="demo profile directory (must be new)")
    args = parser.parse_args()
    base = args.profile or Path(tempfile.mkdtemp(prefix="langgraph-workflow-demo-"))
    base.mkdir(parents=True, exist_ok=True)
    if any(base.iterdir()):
        raise SystemExit(f"{base} is not empty; choose a new demo profile directory")
    print(f"Demo profile: {base}")
    verified_change(base / "change")
    research(base / "research")
    print("\nAll outputs above are fixture data driven through real LangGraph execution.")


if __name__ == "__main__":
    main()
