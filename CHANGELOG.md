# Changelog

## 0.2.0 — 2026-09-24

- Locus plugin (`plugin/`, marketplace in `.agents/plugins/marketplace.json`):
  MCP server with `workflow_start/report/status/cancel`, a skill, and a
  launcher that builds a hash-pinned private venv on first start. Decisions
  are asked through MCP elicitation (Locus's input prompt); no tool can
  answer them.
- `AgentHost`: a host whose jobs are performed by an external agent that
  reports back; changed files are observed by workspace snapshots, file/JSON
  checks are verified by the host.
- New `waiting_for_job` status for hosts that complete jobs asynchronously
  (`running` receipts park instead of being treated as uncertain).
- Fix: an attempt no longer stops early when a parking status left in state
  outlives the step that set it (research fan-out after a report).
- Apache-2.0 license (same as Locus); optional `mcp` extra.

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
