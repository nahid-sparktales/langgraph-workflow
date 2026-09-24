# Changelog

## 0.1.0 — unreleased (local)

- `WorkflowExecutor` with start / status / decide / pause / resume / cancel and
  `asyncio.to_thread` wrappers; inert construction, no background execution.
- Verified-change and read-only research workflows on LangGraph `StateGraph`
  with the SQLite checkpointer (`durability="sync"`).
- `WorkflowHost` port (9 methods), typed JSON contracts, versioned redacted
  events, graph limits that only narrow host limits.
- Replay-safe job protocol, uncertainty parking, deterministic fan-in reducer,
  explicit transition bound, cross-process attempt leases, durable decisions.
- Strict checkpoint deserialization; optional fail-closed encryption with a
  host-injected cipher; LangSmith tracing suppressed per run.
- Durable fixture host, shared host contract suite, process-level CLI, offline
  demo, fixture overhead evaluation.
- Locus reference adapter, headless harness, and the `run_read_job`
  extraction patch (integration harness only).
