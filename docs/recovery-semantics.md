# Recovery semantics

A saved checkpoint is not an atomic external action. LangGraph may re-enter a
node, so every effectful step follows one protocol and recovery never assumes
exactly-once behavior.

## The effect protocol

```text
stable operation_id + input fingerprint (derived from frozen state)
  -> host durably admits the operation
  -> host authorizes and executes it
  -> host durably records outcome / evidence / usage
  -> graph stores the receipt reference (checkpoint, durability="sync")
  -> graph advances
```

On (re-)entry `Runtime.run_job` first calls `host.lookup(operation_id)`:

| Recorded outcome | Action |
| --- | --- |
| none | submit (`execute`) |
| `admitted` (never started) | submit: the host starts it |
| `running` (host still working, e.g. an agent that has not reported) | park as `waiting_for_job`; continue when `lookup` shows an outcome |
| `budget_exhausted`, `busy` (refused, never executed) | submit again: the host re-evaluates |
| `settled`, `failed`, `denied`, `cancelled` | reuse; never execute again |
| `uncertain` (started, outcome unobserved) | park with `uncertain_action`; never replay |
| different input fingerprint | `blocked: operation_conflict`; never execute |

An `uncertain` operation only proceeds after the host records an observed
outcome (fixture: `FixtureHost.reconcile`; Locus: capsule `resolve_action`
semantics). Reconciliation records what happened; it does not authorize
replaying a mutation. A read job whose receipt shows changed files is
`blocked: read_only_violation`.

## Crash windows (tested with real process kills)

| Window | How it is induced | Observed after restart | Test |
| --- | --- | --- | --- |
| Before admission | `LGW_FIXTURE_CRASH=before_admission:write` → `os._exit(137)` | `paused/interrupted`; resume executes the write once | `test_crash_windows_resume_without_repeating_the_write[before_admission]` |
| After admission, before start | `after_admission:write` | resume starts the admitted operation once | `[after_admission]` |
| During the action | `during_action:write` (file half-written) | `uncertain/uncertain_action` on every resume until reconciled; then repair fixes the partial file; effect count stays 1 | `test_crash_during_action_becomes_uncertain_and_is_never_replayed` |
| After receipt, before checkpoint | `after_receipt:write` | receipt reused; effect count 1 | `[after_receipt]` |
| During checkpoint persistence | `LGW_CRASH_ON_CHECKPOINT=after_write` kills inside the checkpoint transaction before `COMMIT` | SQLite rolls back; pending task writes and the host receipt survive; resume continues | `[checkpoint]` |
| SIGKILL mid-job | real `kill -9` while a job runs | started job becomes `uncertain`, not replayed | `test_sigkill_mid_job_then_resume` |

After a torn checkpoint write LangGraph's `get_state().next` can be empty
while work remains; the executor treats only the `finish` node as completion.

The Locus reference adapter maps the same windows onto Locus records: a
`job_attempts` row in `running` without a live owner is `uncertain`; a
`completed` row is reused (`test_verified_change_restart_between_approval_and_execution`
shows no read job repeated after a restart). Its admission and start are one
event, so a crash just before the provider call is conservatively uncertain.

## Decisions

A pending decision carries run/attempt identity, `decision_id`
(`{attempt}:plan:{revision}` or `{attempt}:conflicts:1`), revision, the exact
plan/conflict digest, a bounded summary and allowed options. It is recreated
deterministically on node re-entry, so a restart shows the identical decision.

`WorkflowExecutor.decide` rejects, before resuming: unknown attempt,
cross-run, no pending decision / already consumed, stale decision id, stale
revision or digest, invalid option, `edit` without a plan, cancelled
attempts, unauthorized actors (host), and revoked policy (host
`revalidate`). A decision is recorded once (`lgw_decisions`); an identical
re-delivery after a crash re-applies it, a different answer for the same id
is `duplicate_decision`. An edited plan becomes a new revision with a new
decision. Plan approval never replaces a host tool permission check (the
Locus test shows both).

## Pause, cancel, ownership

- **Pause** is cooperative: `pause()` sets a durable flag; the running
  executor stops after the next persisted super-step. Nothing is suspended
  mid-instruction. `resume()` clears the flag.
- **Cancel** sets a durable flag and calls `host.cancel`; queued branches do
  not start, the next node boundary routes to `finish`, pending decisions are
  closed. `cancelled` is reported only after the host confirms quiescence;
  otherwise the attempt remains `cancel_requested` and cancel can be retried.
- **Ownership**: one executor owns an attempt at a time via a lease row in
  the sidecar database taken under `BEGIN IMMEDIATE`, renewed at each
  checkpoint (default 120 s). A lease from a dead process on the same host is
  ignored; on another host it must expire. A second process, or a second
  thread using the same executor, gets `AttemptBusy`
  (`test_two_processes_cannot_own_one_attempt`,
  `test_one_executor_never_drives_an_attempt_twice`); `decide` refuses while
  another caller is driving, and `cancel` then only sets flags so the driver
  routes to `finish`. The checkpointer itself enforces no ownership. Hosts
  with their own admission leases should hold them as well.
- A repeated `start` for the same attempt resumes; a different request under
  the same attempt id is refused.

## SQLite assumptions

One sidecar file chosen by the host (never inside a source tree, never a
default profile). WAL journal, `synchronous=FULL`, 30 s busy timeout. The
checkpointer uses one connection guarded by its own lock
(`check_same_thread=False`); package tables use short-lived connections so
status/pause/cancel work from other threads and processes. Checkpoints are
written with `durability="sync"`. Local disk only; SQLite over network file
systems is not supported. A held exclusive lock is waited out
(`test_sqlite_busy_is_waited_out`).

## Resume compatibility

Each attempt row records definition version (`verified_change/1`,
`research/1`), state schema version and adapter contract version. A mismatch
raises `IncompatibleAttempt` with an actionable message; the attempt is not
restarted or run under a different graph. Graph-schema migration is separate
from host database migrations. On resume and before any decision the host
revalidates the saved `policy_ref`; revoked permission, a changed checkout or
an unavailable route blocks instead of silently rerouting.

## Retention

`CheckpointStore.prune(older_than_seconds, terminal=(...))` deletes the
checkpoint history of terminal, unleased attempts only; active, paused and
waiting attempts keep full history. Host records (tasks, receipts, usage,
events) are never touched. There is no replay/fork API: reading history and
executing are different operations, and restoring an old checkpoint is never
used to undo external effects.
