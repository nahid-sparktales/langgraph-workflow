# Locus plugin

The repository is a Locus plugin marketplace. Locus downloads it with Git,
shows a trust review, and runs the workflows as an MCP server. The plugin
works with released Locus as is; the optional [window](#the-workflows-window)
needs a Locus build with plugin panels.

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
| `workflow_overview` | Recent runs across projects and the settings in effect. Read-only; used by the window. |
| `workflow_run` | One run for display: steps, plan, pending decision and jobs, checks, activity. Read-only. |
| `workflow_definitions` | Workflows drawn in the window. Read-only. |
| `workflow_definition` | One drawn workflow. Read-only. |
| `workflow_decide` | Answer a pending decision. Window only (see below). |
| `workflow_save_definition`, `workflow_delete_definition` | Save or delete a drawn workflow. Window only. |
| `workflow_launch` | Start a run from the window; decisions wait in the window. Window only. |
| `workflow_dispatch` | The message that hands one assigned step to its agent. Window only. |

The agent has no tool to answer a decision: plan approval and conflict
resolution are asked through Locus's own input prompt
(`mcp_input_required`), so the model cannot approve on your behalf.
The window-only tools are registered only when Locus starts the server with
their names in `LOCUS_PANEL_TOOLS`, which a panel-aware Locus sets after
removing those tools from every agent's tool list; the window is then the
only caller.

## The workflows window

With a Locus build that supports plugin panels (branch `claude/plugin-panels`
in a local Locus worktree; not in released Locus yet), the plugin declares a
window in `.codex-plugin/plugin.json` under `locus.panels`. Open it from
**Agent World → Work → LangGraph Workflows** or from the plugin's row in
**Settings → Extensions**. Older Locus versions ignore the declaration.

- **Runs**: grouped into *Needs you*, *Running* and *Finished*. Each run
  shows its route (Understand → Plan → Approve → Build → Check → Review →
  Done, or Scope → Investigate → Compare → Synthesize → Check → Done), what
  it is waiting for, the plan, the "Done when" checks with their evidence,
  and recent activity. Approve or decline plans and pick conflict answers
  right there; *Continue in chat* opens a Locus chat to hand the agent its
  next job.
- **New workflow**: pick *Make a verified change* or *Research a question*,
  describe the goal, add checks or questions, and preview exactly what the
  agent will receive. *Draft in chat* opens a new Locus chat with it
  drafted; nothing runs until you press Send.
- **Workflows**: draw your own. Add steps from the palette (agent step,
  approval, checks, split, join, end), drag from an outcome to the next step,
  and edit the selected step on the right: what the agent should do, whether
  it may edit files, the outcomes it chooses between, how many times a loop
  may run it, and which saved agent does it. Each agent draws its own line
  color, and the legend shows its provider and model. Start from a template
  (plan–approve–build–check, research in parallel, build with a reviewer).
  Saving validates the graph and explains what to fix.
- **Starting a drawn workflow**: in *New run*, pick it, describe the goal and
  add "Done when" checks, then **Start run**. Locus asks once, in its own
  dialog, whether the plugin may hand this run's steps to the listed agents.
  After that each step goes to its agent's own chat in the project, on that
  agent's model and plan, while the window is open. A step counts as handed
  over only once Locus confirms it was sent; if the agent is still busy with
  its previous step, the window tries again. A window starts runs only in
  its own project: if Locus is showing another project, it says so. Steps without an agent
  are done by whichever chat continues the run.
- **Settings**: approvals, the default review step, limits and how long
  finished runs are kept. Locus validates them against
  `plugin/settings.schema.json` and stores them in the plugin's data folder
  (`locus-settings.json`, mode 0600), where the server reads them for new
  runs.

The window is local HTML (`plugin/ui/`) in Locus's plugin web view: no
network, no storage, `script-src 'self'`. It can only read and save this
plugin's settings, call this plugin's tools, draft a chat, list saved agents
(name, role, provider and model; never instructions, accounts or keys), and
hand steps to agents a run was allowed to use, each gated by a capability the
trust review lists. Locus frames every handed-over message with the plugin's
name and the run, uses the agent's own chat and permissions, and keeps the
per-run permission only while the window is open. Released Locus has no
window: there, a drawn workflow's assigned steps wait until the window is
available, and unassigned steps work as before.

To work on the UI without Locus, serve the repository and open
`tools/panel-preview/index.html`, which fakes the Locus bridge with sample
runs:

```bash
python3 -m http.server 8787
```

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
