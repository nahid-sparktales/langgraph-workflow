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

## Use it in Locus

This repository is a Locus plugin marketplace. In Locus open **Settings →
Extensions → Marketplace**, add `nahid-sparktales/langgraph-workflow`, then
**Review & install** *LangGraph Workflows*. The Locus agent drives each
workflow through the plugin's MCP tools and you approve plans in a Locus
prompt. See [docs/locus-plugin.md](docs/locus-plugin.md) for what it does and
does not guarantee.

## Try it offline

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' -c constraints.txt
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
- [Locus plugin](docs/locus-plugin.md) — install, tools, guarantees, limits
- [Locus handoff](integrations/locus/README.md) — in-process reference adapter, patch, harness, compatibility record

## Requirements

Python ≥ 3.10 (tested: 3.10, 3.12, 3.14). `langgraph>=1.2.2,<1.3`,
`langgraph-checkpoint>=4.1,<5`, `langgraph-checkpoint-sqlite>=3.1,<3.2`.

Development and CI install `constraints.txt`, which pins the LangGraph stack
to what Locus's runtime resolves (`langgraph 1.2.2`; Locus's
`websockets==17.0` rules out newer releases). A separate CI job tests the
newest allowed versions so you can see when upgrading becomes safe.

## License

Apache License 2.0, the same license as Locus. Copyright 2026 SparkTales Inc.
See [LICENSE](LICENSE) and [NOTICE](NOTICE).
