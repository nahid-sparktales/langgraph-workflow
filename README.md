# langgraph-workflow

A small, host-agnostic LangGraph workflow executor. It runs two bounded,
code-defined workflows — **verified change** and **read-only research** — on
real LangGraph graphs with a SQLite checkpoint, and does every effectful
thing (jobs, permissions, verification, events) through a narrow
`WorkflowHost` port implemented by the host runtime. Locus is the first host;
the package never imports it.

```python
from langgraph_workflow import WorkflowExecutor

executor = WorkflowExecutor(host, "/path/in/host/profile/checkpoints.sqlite3")
status = executor.start({
    "workflow": "verified_change", "run_id": "run-1", "task_id": "task-1",
    "attempt_id": "attempt-1", "workspace_id": "ws-1",
    "goal": "Make result.txt say done",
    "checks": [{"id": "result", "kind": "file_contains", "path": "result.txt",
                "value": "done", "requirement": "result.txt says done"}],
})
if status.pending_decision:           # e.g. plan approval
    status = executor.decide({...})   # authenticated host input, revision-checked
```

Only host verification with receipts can yield `verified`; everything else
ends honestly (`needs_review`, `uncertain`, `budget_exhausted`, …).

## Try it offline

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python examples/demo.py          # interrupts, restarts, a crash mid-checkpoint
.venv/bin/python -m pytest -q              # Locus and packaging suites are opt-in
.venv/bin/python examples/evaluate.py      # fixture orchestration overhead -> JSON
```

The demo and tests use a deterministic, durable fixture host
(`langgraph_workflow.testing`); graphs, checkpoints, interrupts and process
restarts are real. They never touch a network, account, or Locus profile.

## Documentation

- [Architecture](docs/architecture.md) — ownership, port, graphs, state, dependency choices
- [Locus integration map](docs/locus-integration-map.md) — source-verified seams at Locus `5ac5b5b1`
- [Recovery semantics](docs/recovery-semantics.md) — effect protocol, crash windows, decisions, leases
- [Security](docs/security.md) — validation, deserialization, encryption, redaction, egress
- [Integration guide](docs/integration-guide.md) — writing a host, consuming a release in Locus, rollback
- [Evaluation](docs/evaluation.md) — what fixtures measure and what they cannot
- [Implementation status](docs/implementation-status.md) — implemented, tested, fixture-only, blocked, deferred
- [Locus handoff](integrations/locus/README.md) — reference adapter, patch, harness, compatibility record

## Requirements

Python ≥ 3.10 (tested: 3.10, 3.12, 3.14). `langgraph>=1.2.2,<1.3`,
`langgraph-checkpoint>=4.1,<5`, `langgraph-checkpoint-sqlite>=3.1,<3.2`.

## License

Not yet chosen by the repository owner; no license is granted. Decide before
distributing or vendoring into Locus (Apache-2.0).
