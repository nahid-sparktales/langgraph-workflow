# Locus integration map

Source-verified against Locus **`5ac5b5b1c450eff0013ed6caae8c696dfc6fb0eb`**
(branch `codex/stabilize-ci-discovery-and-transcript`, 2026-09-23). The
discovery reference `8cf6226d` differs from it only in two test files
(`LocusTests/TranscriptFollowTests.swift`,
`agent/tests/test_mcp_runtime_diagnostics.py`), so every production symbol
below is identical at both revisions. The original checkout was read with
`git archive`/`git diff`/`GIT_OPTIONAL_LOCKS=0 git status` only and remained
clean (0 changed paths) before and after this work.

## Verified architectural facts

| Claim | Evidence at 5ac5b5b1 | Result |
| --- | --- | --- |
| SwiftUI owns presentation/native integrations; Python owns agent execution and local APIs | `Docs/Architecture.md`; `server.py` `create_app()`; `api/*` route modules | Confirmed |
| Teams, scheduling/admission, durable runs, goals, Task Capsules, receipts, recovery, cumulative usage already exist | `orchestration.py` `TeamOrchestrator`, `CrossProcessModelCallScheduler`; `runstore.py` `RunStore` (schema 20); `goals.py`, `goal_runtime.py`; `capsules.py`, `capsule_progress.py`; `task_state.py` `TaskVerifier`; `task_usage_ledger.py`, `usage_ledger.py` | Confirmed; the package owns none of these |
| Python minimum | `agent/pyproject.toml` `requires-python = ">=3.10"`, ruff `target-version = "py310"` | 3.10 |
| Bundled/CI Python | `Tools/PrepareAgentRuntime.sh` `LOCUS_PBS_PYTHON:-3.14.6` (python-build-standalone); `.github/workflows/ci.yml` `python-version: "3.14"` on `macos-15`; lock compiled with Python 3.14 | 3.14.6 bundled; CI 3.14 only |
| Runtime targets | `runtime-packages.yml`: `linux-x86_64`, `linux-arm64`, `macos-arm64`; app bundle macOS arm64 | Three targets |
| Shipped deps are hash-locked, wheel-only | `PrepareAgentRuntime.sh`: `pip install --require-hashes --only-binary=:all: -r requirements-runtime.lock` | Confirmed |
| Edition boundary | `Tools/StageBackendEdition.py` drops `_locusx/`; `Tools/AuditAppEdition.py` scans bundle files/Mach-O for wallet names and symbols | Confirmed; see "Edition audit" |
| A general-purpose Runtime API for external executors | Not present. `/api/runtime/*` is the supervisor controller API for worker processes; `execution_engine` accepts only `locus_managed`/`openai_responses` | **Does not exist**; none is invented here |
| Single writer per checkout | No cross-process file lock in `worktrees.py`; enforced by native `hasLocalWriterCollision` (`AppModel+SendPipeline.swift`), the supervisor's in-memory check (`runtime.py` `coordinate()`), and sequential writer ordering (`ordered_writer_jobs`) | Host-enforced, partly in-process only (limitation below) |

## Responsibility map

| Responsibility | Existing file/symbol | Call/return or event contract | Proposed adapter use | Compatibility test | Remaining limitation |
| --- | --- | --- | --- | --- | --- |
| Run identity and admission | `runstore.py` `RunStore.start_run`, `queue_run`, `admit`, `set_state`; states: terminal `completed/failed/interrupted/cancelled/discarded`, recoverable `paused/interrupted/waiting_dispatch_approval`, active `queued/dispatching/running/reviewing/pausing/waiting_permission/waiting_computer` | `start_run(run_id, *, session_id, workspace_root, execution_path, request, state)`; state changes only via `orchestration_*` events | `LocusHost.admit` creates/reuses the run and task record; the package never sets run state | `test_locus.py::test_verified_change_restart_between_approval_and_execution` | Reference adapter starts runs directly; Prompt 2 must go through the app's queue/admission path and the independent-runtime supervisor when applicable |
| Logical task identity | `task_journal.py` `TaskJournal.bind/for_owner` (`run:`/`capsule:`/`goal:`/`session:` owners) | Returns journal with `task_id`; `None` in identity mode | Bound to `core.task_journal` so writer receipts and usage attach to the task | Same test (`usage_summary` entries) | Identity-mode chats have no journal; workflows must refuse identity sessions (Prompt 2) |
| Bounded read-only job | `orchestration.py` `TeamOrchestrator._call_agent` (private) → **patch 0001** `run_read_job(run_id, job, profile, budget) -> AgentResult` | Emits `agent_job_started`/`agent_job_completed`; raises `InterruptedError` on stop, `OrchestrationError` on budget; no tools offered | `LocusHost._read`; job id `"{operation_id}#{input_fingerprint}"` | `test_parallel_read_only_research`, `test_without_extraction_patch_read_jobs_wait_for_capability`, Locus `test_run_read_job_is_the_one_job_specialist_path` | Needs the 39-line extraction; without it the adapter withholds `jobs.read` |
| Bounded writer job | `core.py` `AgentCore.run_turn(user_text, decider, *, allow_tools, model_call_limit, persist_user_message)`; team path `server._run_team_writer` (needs `ChatService` + `TeamPreparation`) | Result in `core.last_turn_result` (`reason`, `model_calls`); tool effects in the workspace | `LocusHost._write` runs one `run_turn` slice and records a `job_attempts` row around it | `test_verified_change_restart...`, `test_denied_write_is_honest` | Does not reuse `_run_team_writer`'s scheduler `writer_slot`, goal attachment, or writer-route install; Prompt 2 should extract a writer seam from `server.py` into a feature module instead of calling `run_turn` directly |
| Durable job/receipt identity | `RunStore.append_event` → `_update_attempt` → `job_attempts` (`attempt_id = "{run}:{job}:{n}"`); `RunStore.attempts(run_id)` | `agent_job_started` inserts `running`; `agent_job_completed` sets state + `result_json` | `lookup()` reads `attempts()`; `running` without an in-process owner ⇒ `uncertain`; never re-executed | Contract `execute_is_idempotent`, `fingerprint_conflict_is_not_executed`; `test_verified_change_restart...` (`reader_calls == []` after restart) | Admission and start are one event in the reference adapter, so a crash before the provider call is still reported as uncertain (conservative) |
| Model call accounting | `_raw_call`: `task_usage_ledger.UsageLedger.reserve/settle` (idempotent id), `model_usage.tracked_chat` (`usage_ledger.begin/settle/uncertain`) | Pending before dispatch; settled after; unknown stays unknown | Adapter reports usage as display-only (`authoritative: false`); Locus ledgers stay authoritative | `usage_summary.entries` all `settled` in the Locus test | Writer-turn usage is recorded by Locus but not summarized back per operation |
| Model-call concurrency | `CrossProcessModelCallScheduler` (SQLite lease DB under `app_dir()`, limit `LOCUS_MODEL_CALL_LIMIT`=3, 660 s leases) | `scheduler_lease_*` events | Read jobs go through it automatically via `run_read_job` | `event_types` include `scheduler_lease_*` | Graph `max_parallel_reads` (2) is an additional, narrower bound |
| Permission decision | `permissions.py` `PermissionManager` (modes `ask/accept_edits/bypass`); `core._execute_tool_call` emits `permission_request` then calls `decider(tool, summary, detail, request_id) -> once/always/deny` | Denied tools are not executed; receipt `executed: false` | Adapter passes its decider through; plan approval never substitutes for a tool decision | `permission_decisions == [["write_file", "once"]]`; deny ⇒ `job_denied`, file absent | Reference decider is a callable; Prompt 2 must use `ChatService.decide` / durable `runtime_decisions` |
| Verification and evidence | `task_state.py` `TaskStateStore.ensure/receipts/completion`, `TaskVerifier.verify(checks, decider)` (kinds `file_exists`, `file_contains`, `json_value`, `command`, `human_review`) | Receipts with `id`, `check_hash`, `revision`, `fingerprints`, `state`; `completion()` detects stale fingerprints/revisions | `verify(final=False)` runs `TaskVerifier`; `verify(final=True)` answers from `completion()` only when Locus proves evidence current, else re-runs | `test_file_changed_after_verification_is_not_verified`; contract `failed_check_is_failed_with_receipt`, `human_review_is_never_passed` | `TaskVerifier` requires a live `AgentCore` (tool path + session); command checks run through bash permissions |
| Event delivery and cursor | `RunStore.append_event` (assigns `seq`, keeps a supplied `event_id` as PRIMARY KEY), `events(run_id, after_seq, limit)`; `GET /api/orchestrations/{id}/events?after_seq=` | Durable envelope `event_id, seq, occurred_at, schema_version`; clients dedupe by `event_id` | Publishes one namespaced `workflow_event` per package event with the package's deterministic `event_id`; duplicate ⇒ `IntegrityError` ⇒ ignored | `events_after_duplicate_publish == 0`; `replay_from_cursor`; contract `publish_deduplicates_event_id` | Native UI shows `workflow_event` rows with a generic title; live handler ignores them (`default: break`). Presentation is Prompt 2 |
| Native event decoding | `Locus/AgentTeams.swift` `OrchestrationEvent` decodes any object; `AppModel+BackendEvents.swift` switch has `default: break`; `PROTOCOL.md` "Older clients may ignore all unknown events and fields" | Tolerant | No existing event names or payloads are changed | Source inspection only | No Swift test was run in this phase |
| Cancellation | `core.interrupt()` (`threading.Event`), `TeamOrchestrator.should_stop`, `stop_branch`; API `POST .../cancel` sets `cancel_requested_runs` | Streams and subprocesses observe stop; `InterruptedError` | `LocusHost.cancel` sets its stop flag and `core.interrupt()`, waits for in-flight jobs; reports quiescence | Contract `idle_cancel_is_quiescent`, `no_new_job_starts_after_cancel` | Active cancellation mid-provider-stream against Locus is not exercised (fixture only) |
| Memory | `memory.py`, `memory_runtime.py`, `knowledge.py` (approved memories, candidates) | Scoped memory is injected by Locus into agent prompts | Package has no memory port; jobs receive Locus-scoped context through Locus's own job path | n/a | Proposing memory candidates from workflows is deferred |
| Independent runtime / controller disconnect | `runtime.py` `RuntimeSupervisor` (`keep_running`, 35 s heartbeat, `uncertain` commands never replayed) | Ordinary work pauses on disconnect | The executor never runs in the background; it only advances inside a host call | Package `test_import_and_construction_are_inert`, sync-call design | Supervisor integration is Prompt 2 |
| Workspace single writer | See facts table | In-process checks only | Graph never runs parallel writers; fixture host enforces `busy`; Locus adapter relies on Locus | Package `test_concurrent_writers_on_one_workspace_are_serialized` (fixture) | No cross-process per-checkout lock exists in Locus Python today |

## Traced task (real code, deterministic provider)

`tests/integration/test_locus.py::test_verified_change_restart_between_approval_and_execution`
drives one verified change through Locus at 5ac5b5b1 + patch 0001:

1. **Admission** — `LocusHost.admit` → `RunStore.start_run("run-lgw", state="running")`,
   `TaskStateStore.ensure("work:lgw", revision=1, execution=<workspace>)`,
   `TaskJournal.bind` → returns `Admission(policy_ref=digest(permissions, reader route, writer model, workspace))`.
2. **Model call** — inspect/plan jobs → `TeamOrchestrator.run_read_job` →
   `_call_agent` → `_raw_call` → scheduler lease (`scheduler_lease_waiting/acquired/released`)
   → `task_usage_ledger.reserve` → `tracked_chat` (scripted provider) → `settle`
   → `agent_job_completed` → `job_attempts` row `completed`.
3. **Decision** — plan approval interrupt; process exits; a new process with
   fresh `AgentCore`/`RunStore` objects reads the same pending decision; a
   forged actor is rejected (`unauthorized`); the controller's approval resumes.
4. **Tool call and permission** — writer job → `AgentCore.run_turn` → model
   proposes `write_file` → `permission_request` → decider returns `once` →
   tool executes → per-invocation receipt in `task_observations`.
5. **Usage record** — read-job calls are `settled` entries in the task usage
   ledger; the writer turn's calls are accounted by Locus's core path.
6. **Verification** — `TaskVerifier.verify` → `read_file` through the tool
   path → receipt `passed` with fingerprints; final pass answered by
   `TaskStateStore.completion()` because evidence is provably current;
   Locus's own `completion()` returns `passed`.
7. **Event delivery** — 12 `workflow_event` rows with unique ids interleaved
   with native `agent_job_*`/`scheduler_lease_*` events; re-publishing is a
   no-op; replay from a cursor returns the tail.
8. **Recovery** — after the restart no read job is repeated (`reader_calls == []`),
   the orchestrator's call counter is re-seeded from durable `job_attempts`,
   and the graph resumes from its sidecar checkpoint.

## Proposed status mapping (not wired in this phase)

Locus separates run lifecycle (`runs.state`) from task verification
(`task_records.verification_status`). The adapter should keep that split:

| Package status | Locus run state | Locus task verification / metadata |
| --- | --- | --- |
| `running` | `running` | `checking` while verifying |
| `waiting_for_input` | `paused` (recoverable) | pending decision in metadata; not `waiting_dispatch_approval`, whose payload contract differs |
| `paused` | `paused` | — |
| `waiting_for_capability`, `budget_exhausted`, `uncertain` | `paused` | blocker code; uncertainty uses the existing capsule `resolve_action` semantics |
| `cancel_requested` | unchanged + `cancel_requested_runs` | draining |
| `cancelled` | `cancelled` | — |
| `denied` | `cancelled` | `plan_denied` |
| `failed`, `blocked` | `failed` | blocker code (`transition_limit`, `incompatible_definition`, `policy_revoked`, …) |
| `needs_review` | `completed` | `needs_review` |
| `verified` | `completed` | `passed` |
| explicit human acceptance | `completed` | `accepted` via existing `POST /api/sessions/{id}/task/accept` |

## Smallest extraction

`integrations/locus/patches/0001-expose-bounded-read-job-seam.patch`
(sha256 `248dc648dd9ee658e78b6c1289e422b841cb25a8a412881b53597d57977945ca`)
adds `TeamOrchestrator.run_read_job`, a public delegate to `_call_agent` that
refuses `execution_kind="write"`, plus one Locus test. It is a pure refactor
(no behavior change). Applied as its own commit in the disposable checkout;
the full Locus backend suite results with and without it are in
`integrations/locus/compatibility.json`.

The writer seam needs a larger extraction (moving a single-writer-slice
function out of `server.py` into a feature module with the scheduler slot,
goal attachment and route install). That belongs with Prompt 2's adapter and
is intentionally not done here.

## Edition audit

`Tools/AuditAppEdition.py` patterns (`WALLET_FILES`, `WALLET_CODE`, Mach-O
`strings`) were run over every file of the 24 distributions the package adds
to the Locus runtime plus the package itself: 1,837 files, 8 Mach-O
binaries, **0 matches**. The package imports no `_locusx` code and reads no
edition environment variables.
