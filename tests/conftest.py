import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from langgraph_workflow import WorkflowExecutor
from langgraph_workflow.testing import FixtureHost

CHECK = {"id": "result", "kind": "file_contains", "path": "result.txt", "value": "done",
         "requirement": "result.txt says done"}


def change_request(attempt="att-1", **over):
    request = {"workflow": "verified_change", "run_id": "run-1", "task_id": "task-1",
               "attempt_id": attempt, "workspace_id": "ws-1",
               "goal": "Make result.txt say done", "checks": [CHECK]}
    request.update(over)
    return request


def research_request(attempt="res-1", **over):
    request = {"workflow": "research", "run_id": "run-2", "task_id": "task-2",
               "attempt_id": attempt, "workspace_id": "ws-1",
               "goal": "Which storage engine should the cache use?"}
    request.update(over)
    return request


def respond(status, choice="approve", actor="user", **extra):
    pending = status.pending_decision
    return {"decision_id": pending["decision_id"], "run_id": status.run_id,
            "attempt_id": status.attempt_id, "revision": pending["revision"],
            "digest": pending["digest"], "choice": choice, "actor": actor, **extra}


@pytest.fixture
def make(tmp_path):
    executors = []

    def factory(scenario=None, root=None):
        root = Path(root or tmp_path)
        host = FixtureHost(root, scenario)
        host.save_scenario()
        executor = WorkflowExecutor(host, root / "checkpoints.sqlite3")
        executors.append(executor)
        return host, executor

    yield factory
    for executor in executors:
        executor.close()


def run_cli(root, *args, env=None, timeout=60):
    """One executor call in a fresh interpreter (a real process restart)."""
    completed = subprocess.run(
        [sys.executable, "-m", "langgraph_workflow.testing", str(root), *map(str, args)],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    out = completed.stdout.strip().splitlines()
    return completed.returncode, (json.loads(out[-1]) if out else None), completed.stderr


def write_json(path, value):
    Path(path).write_text(json.dumps(value))
    return path
