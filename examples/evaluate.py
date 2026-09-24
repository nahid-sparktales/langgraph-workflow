"""Orchestration-overhead evaluation with deterministic fixtures.

    python examples/evaluate.py [--runs 20] [--out evaluation/fixture-overhead.json]

Measures only what fixtures can measure: graph setup, checkpoint writes, host
calls, and wall time of the orchestration itself (fixture jobs take ~0 ms).
It says nothing about model quality, provider latency, token usage, or cost;
live comparisons need a separately authorized campaign (docs/evaluation.md).
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from collections import Counter
from importlib import metadata
from pathlib import Path

from langgraph_workflow import WorkflowExecutor
from langgraph_workflow.testing import FixtureHost

CHECK = {"id": "result", "kind": "file_contains", "path": "result.txt", "value": "done",
         "requirement": "result.txt says done"}
SCENARIOS = {
    "change_supplied_plan": (
        {"writes": {"implement": {"result.txt": "done\n"}}},
        {"workflow": "verified_change", "goal": "g", "checks": [CHECK],
         "plan": {"steps": [{"title": "write"}]}}, "verified"),
    "change_full_with_repair_and_review": (
        {"review": ["changes_requested", "approve"],
         "writes": {"implement": {"result.txt": "x\n"},
                    "repair": [{"result.txt": "done\n"}, {"result.txt": "done 2\n"}]}},
        {"workflow": "verified_change", "goal": "g", "checks": [CHECK], "reviewer": True},
        "verified"),
    "research_three_parallel": (
        {"findings": {str(i): {"claims": {f"k{i}": i}, "sources": [f"s{i}"]} for i in range(3)},
         "synthesis": {"claims": {"k0": 0}}},
        {"workflow": "research", "goal": "g", "investigations": ["a", "b", "c"],
         "checks": [{"id": "n", "kind": "file_exists", "path": "notes.md", "requirement": "n"}]},
        "verified"),
}


class CountingHost:
    def __init__(self, host: FixtureHost) -> None:
        self._host, self.calls = host, Counter()

    def __getattr__(self, name):
        attr = getattr(self._host, name)
        if not callable(attr):
            return attr

        def counted(*args, **kwargs):
            self.calls[name] += 1
            return attr(*args, **kwargs)
        return counted


def run_once(root: Path, name: str, index: int) -> dict:
    scenario, request, expected = SCENARIOS[name]
    host = CountingHost(FixtureHost(root, scenario))
    (root / "workspace" / "notes.md").write_text("n")
    executor = WorkflowExecutor(host, root / "checkpoints.sqlite3")
    request = {**request, "run_id": "eval", "task_id": "eval", "workspace_id": "eval",
               "attempt_id": f"{name}-{index}"}
    started = time.perf_counter()
    executor.store.open()
    executor._graph(request["workflow"])
    setup = time.perf_counter() - started
    status = executor.start(request)
    total = time.perf_counter() - started
    executor.close()
    with sqlite3.connect(root / "checkpoints.sqlite3") as db:
        checkpoints = db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
        writes = db.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
    return {"scenario": name, "run": index, "status": status.status,
            "correct": status.status == expected, "setup_s": setup, "total_s": total,
            "checkpoints": checkpoints, "pending_writes": writes,
            "db_bytes": sum(p.stat().st_size for p in root.glob("checkpoints.sqlite3*")),
            "host_calls": dict(host.calls), "jobs": len(status.jobs)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--out", type=Path, default=Path("evaluation/fixture-overhead.json"))
    args = parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in SCENARIOS:
            for index in range(args.runs + 1):  # first run is a discarded warm-up
                row = run_once(Path(tmp) / f"{name}-{index}", name, index)
                row["warmup"] = index == 0
                rows.append(row)
    summary = {}
    for name in SCENARIOS:
        measured = [r for r in rows if r["scenario"] == name and not r["warmup"]]
        totals = sorted(r["total_s"] for r in measured)
        summary[name] = {
            "runs": len(measured), "correct": sum(r["correct"] for r in measured),
            "median_total_ms": round(statistics.median(totals) * 1000, 2),
            "p95_total_ms": round(totals[int(0.95 * (len(totals) - 1))] * 1000, 2),
            "median_setup_ms": round(statistics.median(r["setup_s"] for r in measured) * 1000, 2),
            "checkpoints": measured[0]["checkpoints"], "jobs": measured[0]["jobs"],
            "host_calls": measured[0]["host_calls"],
        }
    result = {
        "kind": "fixture orchestration overhead (not a live-model evaluation)",
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "langgraph": metadata.version("langgraph"),
                        "langgraph-checkpoint-sqlite":
                            metadata.version("langgraph-checkpoint-sqlite")},
        "summary": summary, "raw": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1))
    print(json.dumps({"environment": result["environment"], "summary": summary}, indent=1))


if __name__ == "__main__":
    main()
