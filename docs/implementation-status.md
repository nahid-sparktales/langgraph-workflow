# Implementation status

Recorded 2026-09-24 for package 0.1.0 (local, unpublished). "Tested" means a
test in this repository ran and passed; results and commands are in
`integrations/locus/compatibility.json`.

## Implemented and tested (package, fixture host)

- Verified-change graph: supplied-plan skip, inspect/plan jobs, plan approval
  interrupt (approve / deny / edit → new revision), writer job, host
  verification with evidence floor, bounded repair, no-progress detection,
  configured reviewer with repair loop, final verification, missing
  capability, not admitted, denied jobs, host and graph budget exhaustion,
  transition bound, invalid plans, forged/missing evidence, human-review,
  unsupported and denied checks.
- Research graph: deduplicated bounded scope, up-front job reservation,
  `Send` fan-out bounded to 2 concurrent reads, deterministic fan-in,
  provenance and conflict detection, conflict decision interrupt, read-only
  violation, denied investigation, synthesized-claim provenance check.
- Executor: inert construction, start/resume/decide/pause/cancel/status,
  async wrappers, event-loop guard, cooperative pause at persisted
  super-steps, cancellation with quiescence, cross-process and same-process
  ownership guard, duplicate start, incompatible definitions.
- Recovery: interrupt + restart + resume in new processes for both
  workflows; crash before admission, after admission, during the action,
  after the receipt, inside checkpoint persistence, and SIGKILL mid-job.
- Decisions: stale, edited, duplicate, cross-run, unauthorized, revoked,
  restart while waiting.
- Privacy/security: planted secrets absent from events, metadata and sidecar
  tables; encrypted store free of plaintext incl. WAL; fail-closed
  encryption; strict deserialization; diagnostics opt-in; LangSmith egress
  suppressed with a positive control; symlink escape refused by host path
  logic; no environment/logging side effects.
- Shared host contract (17 checks) passing for the fixture host.
- Packaging: wheel installs into a fresh venv and runs the demo outside the
  checkout.

## Tested against real Locus code (5ac5b5b1 + patch 0001, deterministic provider)

- Host contract (17 checks) against `LocusHost`.
- Verified change across a process restart at the approval interrupt:
  real `run_read_job` jobs, scheduler leases, usage ledger entries,
  `AgentCore.run_turn` writer with a real permission request and decider,
  `TaskVerifier` receipts, final evidence proved current via `completion()`,
  forged actor rejected, event dedupe and cursor replay, no repeated jobs.
- Denied write → `needs_review`; file changed after verification →
  `needs_review/final_checks_failed`; parallel read-only research verified.
- Without patch 0001: `jobs.read` withheld, workflows wait for the capability.
- Locus's own backend suite with the package installed, with and without the
  patch (counts in `compatibility.json`).
- Co-installation with Locus's hash-locked runtime (`--require-hashes`,
  `--only-binary=:all:`) resolving `langgraph 1.2.2` without moving any pin;
  edition audit of all added files with zero wallet matches.

## Locus plugin (0.2.0)

- Tested against real Locus code (disposable checkout, deterministic): Locus's
  `ExtensionManager` adds the marketplace, shows the trust review, installs
  with digest verification; Locus's `MCPManager` starts the plugin (launcher
  first run builds the venv), lists the four tools, and a verified change
  completes with the plan approval answered through Locus's
  `mcp_input_required` prompt path.
- Tested over real stdio with `mcp.Client` (legacy protocol): elicited
  approval, declined-then-answered decision, server restart, forged
  operation ids.
- `AgentHost`: read-only violations, no-progress, single-use reports,
  parallel investigations reported one at a time, cancellation, command
  checks ending in `needs_review`.
- Not verified: installing through the Locus app UI by hand, a real model
  following the skill, Linux, and first-run installs on slow networks
  (120-second startup limit).

## Workflows window (0.3.0)

- Locus side lives on a local, unpushed Locus branch (`claude/plugin-panels`):
  manifest parsing and validation of `locus.panels`, settings API with schema
  validation and revision checks, panel-only tools hidden from agents and
  callable over REST, and the native window (WKWebView, `locus-screen://`
  scheme). Tested with Locus's Python suite and Swift unit tests
  (`PluginPanelTests`, `AgentWorldTests`, `ExtensionsModelTests`,
  `SocialStudioTests`).
- Plugin side: `workflow_overview`, `workflow_run` and `workflow_decide` over
  real stdio, saved settings applied to new runs, schema defaults equal to the
  server's.
- UI checked in a browser against a fake bridge
  (`tools/panel-preview/`): light and dark, the 760 px minimum window width,
  approve flow, compose, settings save and reset.
- Not verified: the window opened inside a running Locus app by hand, and
  VoiceOver.

## Fixture-only (not exercised against Locus)

- Cancellation of an active writer and queued parallel jobs, workspace
  single-writer `busy`, SQLite busy wait, retention pruning, the crash
  windows other than "restart at an interrupt", encryption.
- Model token streams: Locus's `agent_job_stream` events are held in a
  bounded in-memory buffer by the reference adapter and not re-published;
  no UI consumed them in this phase.

## Blocked or not verified

- **Linux arm64**: dependency resolution to binary wheels verified; not
  executed. Linux x86_64 runs in CI (package suite only, not inside a Locus
  runtime package).
- **CI** (GitHub Actions run 36053254108, commit `da6c1fe`): all 7 jobs
  passed — pinned Locus resolution on Ubuntu with Python 3.10, 3.11, 3.12,
  3.13, 3.14 and on macOS 15 with 3.14 (83 passed, 7 opt-in skips each;
  packaging test passed on both 3.14 jobs), and the newest allowed versions
  (`langgraph 1.2.12`) on Ubuntu 3.14.
- **Signed/notarized macOS bundle**: the 8 added native libraries were not
  signed or notarized; no app build was made.
- **Live providers, subscription sessions, paid accounts, remote runtime,
  SSH**: not exercised. No paid or external action was performed.
- **Native Swift UI**: no Swift build or test ran; `workflow_event`
  presentation does not exist yet.
- **`pip-audit`** of the added dependencies: not run.

## Deferred (by design for the first release or to Prompt 2)

- Production adapter inside Locus, executor selection, feature flag, lazy
  import and capability reporting, native presentation, status mapping.
- Writer-slice extraction from `server._run_team_writer` (scheduler writer
  slot, goal attachment, route install) and durable decisions via
  `ChatService`/`runtime_decisions`.
- Scoped memory view / memory candidates, optional external tracing,
  replay/fork controls, visual editor, user-supplied graphs, recursive
  delegation, learned routing.
