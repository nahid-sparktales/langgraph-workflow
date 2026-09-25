# Changelog

## 0.4.1 — 2026-09-25

- Fix: the plugin failed to start inside the Locus app ("MCP server closed the
  connection during connect/initialize"). The Python bundled with Locus has
  neither pip nor ensurepip, so creating the plugin's environment failed. The
  plugin now ships a checksummed pip wheel, creates its environment without
  ensurepip, and verifies every vendored wheel before using it.

## 0.4.0 — 2026-09-24

- Drawn workflows (`workflow="custom"`): a graph of agent steps, approvals,
  checks, parallel read-only branches and bounded loops, validated as plain
  data (`definitions.py`) and run by one interpreter node
  (`workflows/custom.py`) inside a fixed LangGraph graph. Runs pin their copy.
- Agent assignment: each step (or the whole workflow) can name a saved agent;
  `JobSpec.assignee` carries it and `AgentHost.dispatch` hands the job over
  with a claim that the report must carry, so other chats cannot do it.
- Locus window: a Workflows tab with a canvas editor (palette, drag to
  connect, inspector, templates, agent line colors), starting drawn workflows
  from the window with one Locus confirmation per run, and automatic
  hand-offs to each step's agent while the window is open.
- MCP tools `workflow_definitions`, `workflow_definition` (read-only) and, for
  the window only, `workflow_save_definition`, `workflow_delete_definition`,
  `workflow_launch`, `workflow_dispatch`; `workflow_report` takes `claim`.
- A step counts as handed over only after Locus confirms delivery
  (`workflow_dispatch(delivered=true)`); a refused hand-off (agent busy) is
  retried. Runs started from a window start only in that window's project,
  and Locus refuses hand-offs into another project's chats.
- Parallel branches reported by two chats at once no longer strand a run:
  the second report waits for the first, and the window moves on any run
  whose job was reported without a resume.

## 0.3.0 — 2026-09-24

- Workflows window for Locus builds with plugin panels (`locus.panels` in the
  plugin manifest, `plugin/ui/`): follow runs on a step route, approve plans
  and answer conflicts, draft new workflows into a chat, and edit settings.
  Released Locus ignores it.
- Settings (`plugin/settings.schema.json`, stored by Locus as
  `locus-settings.json`): plan and conflict approval, default review step,
  limits, and pruning of finished runs after N days.
- MCP tools `workflow_overview` and `workflow_run` (read-only), and
  `workflow_decide`, registered only when Locus hides it from agents
  (`LOCUS_PANEL_TOOLS`).
- `AttemptStatus` gains `goal` and `updated_at`; `CheckpointStore.attempts()`
  lists recent attempts.

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
