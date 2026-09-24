# Locus plugin

The repository is a Locus plugin marketplace. Locus downloads it with Git,
shows a trust review, and runs the workflows as an MCP server. No Locus code
changes are needed.

## Install

1. In Locus: **Settings → Extensions → Marketplace**, enter
   `nahid-sparktales/langgraph-workflow` (or a local clone's folder) and
   choose **Add source**.
2. On **LangGraph Workflows**, choose **Review & install**. The review lists
   the MCP server command (`/bin/sh ${PLUGIN_ROOT}/bin/launch`), the skill,
   and the tree digest; install for one workspace or everywhere.
3. The first time the server starts, the launcher builds a private virtual
   environment under the plugin's data folder from `plugin/requirements.lock`
   (hash-pinned wheels from PyPI, so it needs network once) plus the vendored,
   checksummed `langgraph-workflow` wheel. Later starts reuse it.
4. Ask the Locus agent for a verified change or a research task; the
   `langgraph-workflow` skill tells it how to use the tools.

Updates: publish a new plugin version in the repository, then use **Review
update** in Locus. Uninstalling the plugin leaves its data folder
(checkpoints and venvs) in Locus's extension data directory.

## How it works

```text
Locus agent ──tools──▶ MCP server (plugin, child process)
   │  does each job with its own     │  LangGraph graph + SQLite checkpoint
   │  tools and permissions          │  AgentHost: job ledger, workspace
   ▼                                 │  snapshots, file/JSON checks
workspace ◀──── reads, never writes ─┘
you ◀── plan approval / conflict choice (Locus prompt via MCP elicitation)
```

| Tool | Purpose |
| --- | --- |
| `workflow_start` | Start `verified_change` or `research` in the open workspace with acceptance `checks`, optional `plan_steps`, `investigations`, `reviewer`. Returns the first job(s). |
| `workflow_report` | Report one job's outcome (`completed`, `failed`, `refused`) and result; returns the next job, a decision prompt, or the result. |
| `workflow_status` | Current state; re-asks a pending decision. Read-only. |
| `workflow_cancel` | Cancel; pending jobs are closed. |

There is deliberately no tool to answer a decision: plan approval and
conflict resolution are asked through Locus's own input prompt
(`mcp_input_required`), so the model cannot approve on your behalf.

## What is and is not guaranteed

- **Actions** are performed by the Locus agent through Locus's tool loop, so
  Locus's permission modes and prompts apply. The plugin runs no model, tool
  or shell command.
- **Changes are observed, not trusted.** The plugin snapshots the workspace
  when it hands out a job and when the job is reported (Git-tracked and
  untracked files, or a walk skipping build/dependency folders, up to 4,096
  files). A read job that changed files blocks the workflow
  (`read_only_violation`); a writer that changed nothing counts as no
  progress.
- **Verification** of `file_exists`, `file_contains` and `json_value`
  checks is done by the plugin on the current files, with a receipt.
  `command` checks are not run by the plugin (that would bypass Locus
  permissions) and `human_review` checks need you, so both end in
  `needs_review`. The agent's prose never counts as evidence.
- **Recovery**: state survives Locus and plugin restarts. Each job is handed
  out once; a job that was never reported stays pending instead of being
  given out again.
- **Not provided in plugin mode**: Locus's own receipts (`TaskVerifier`),
  usage ledger per workflow job, and run-history presentation. Those need the
  in-process adapter in `integrations/locus/` (a Locus change). Model usage
  for the agent's turns is still accounted by Locus as ordinary chat usage.

## Requirements and limits

- Python 3.10+ for the server. The launcher prefers the interpreter that runs
  Locus's agent server (Python 3.14 in the app), then `python3.14`…`python3.10`
  and `python3` on `PATH`, `/opt/homebrew/bin` and `/usr/local/bin`. Set
  `LGW_PYTHON` to override.
- The first start needs network access to PyPI and must finish within
  Locus's 120-second MCP startup limit; if it times out, the next start
  resumes with pip's cache. Builds are atomic, so a partial venv is never
  used.
- `protocol_mode` is `legacy`: with MCP 2.x, decision prompts and workspace
  roots use the stdio back-channel of the legacy protocol. Under the modern
  protocol the server reports that it cannot show prompts instead of
  stalling silently.
- Locus's plugin limits (5,000 files, 250 MB) apply to the 0.2 MB plugin
  folder, not to the venv, which lives in the plugin's data folder.

## Maintaining

After changing `src/` or `plugin/requirements.in`:

```bash
.venv/bin/python tools/build_plugin.py   # vendored wheel, SHA256SUMS, universal hashed lock
.venv/bin/python -m pytest -q tests/unit/test_plugin.py
```

`test_plugin.py` fails if the vendored wheel no longer matches `src/`.
`tests/integration/test_locus.py::test_plugin_installs_from_marketplace_and_runs_in_locus`
installs the plugin with Locus's `ExtensionManager`, starts it with Locus's
`MCPManager`, and completes a verified change with the approval answered
through Locus's prompt path (`LGW_PLUGIN_SOURCE=owner/repo` tests the GitHub
download instead of a local folder).
