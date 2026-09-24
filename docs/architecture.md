# Architecture

`langgraph-workflow` is an in-process Python library. It owns workflow
definitions, graph construction, graph-level scheduling inside a
host-admitted attempt, the graph checkpoint, a small typed host contract, and
event normalization. It owns nothing else.

```text
host UI and transports (unchanged)
            |
host runtime: identity, admission, accounts, permissions, usage ledger,
tools, memory, evidence, workspaces, process ownership
            |
      executor selection (host decides; native path stays the default)
        /                 \
native path          host adapter (implements WorkflowHost)
                            |
                     langgraph_workflow.WorkflowExecutor
                            |
                     LangGraph StateGraph + SQLite checkpointer (sidecar)
                            |
                     WorkflowHost port calls
                            |
                     host's existing job / tool / verification path
```

The dependency points one way: an adapter imports `langgraph_workflow`; the
package never imports a host. Locus is the first host
(`integrations/locus/adapter_reference.py`); any runtime that can implement
the nine-method port can use the same workflows.

## Modules

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Plain dataclasses and validation for requests, admission, jobs, receipts, verification, decisions, status. No LangGraph types. |
| `ports.py` | `WorkflowHost` protocol and capability names. |
| `executor.py` | `WorkflowExecutor`: start, resume, decide, pause, cancel, status; async wrappers; drives the graph. |
| `state.py` | Graph state base, fan-in reducer, the replay-safe job helper, evidence floor, transition bound. |
| `workflows/verified_change.py`, `workflows/research.py` | The two graphs. |
| `checkpoints.py` | SQLite checkpointer setup, strict/encrypted serializer, sidecar tables (attempts, leases, controls, consumed decisions), retention. |
| `events.py` | Versioned event contract and redaction. |
| `policy.py` | Graph limits and their intersection. |
| `testing.py` | Durable fixture host, host contract suite, process-level CLI. |

LangGraph's `Command`, `Send`, `interrupt`, state channels and stream chunks
stay inside `executor.py`, `state.py` and `workflows/`.

## The host port

```python
class WorkflowHost(Protocol):
    def capabilities(self) -> frozenset[str]
    def admit(self, request) -> Admission                  # idempotent per attempt
    def revalidate(self, request, policy_ref) -> Admission # before any resume/decision
    def execute(self, spec: JobSpec) -> JobReceipt         # idempotent by operation_id
    def lookup(self, operation_id) -> JobReceipt | None
    def cancel(self, attempt_id) -> bool                   # True once quiescent
    def verify(self, request, checks, *, final) -> VerificationReport
    def authorize_decision(self, response) -> bool
    def publish(self, event: WorkflowEvent) -> None        # dedupe by event_id
```

`execute` is the central capability: a bounded job that the host admits,
authorizes and runs through its own job path, returning a job identity the
host can answer for after a restart. The package never holds credentials,
clients, or tools.

## Workflows

**Verified change** — `validate → inspect → plan → approve → implement →
verify → [repair → verify]* → [review → repair?]* → finalize → finish`.
A supplied plan skips `inspect`/`plan`. A configured reviewer always runs and
must approve against current verification; a repair after review invalidates
the review. Every stop routes through `finish`. Resumable stops (uncertain
action, busy workspace, exhausted budget, missing capability) re-target the
same node, so a resume re-queries the host instead of replaying.

**Research** — `validate → scope → investigate×N (Send, ≤ max_parallel_reads
concurrently) → collect → [resolve] → synthesize → verify → finish`. Scope
deduplicates questions, bounds their number, and reserves the whole fan-out
plus synthesis against `max_jobs` before any branch starts. Collect merges in
index order, records provenance and conflicts deterministically. Every job is
`access="read"`; a receipt with changed files is a `read_only_violation`.

## State and identity

- Thread id = host `attempt_id`. A true resume keeps it; a new execution
  gets a new one. A chat session is not a thread.
- `operation_id = "{attempt_id}/{node key}"`; repair/review keys include the
  round and implement includes the approved plan digest, so an edited plan is
  a different operation. Two legitimately identical jobs in different rounds
  are distinct operations.
- State holds the frozen request, admission (policy ref, approval policy),
  effective limits, definition/schema/contract versions, plan + revision +
  digest, receipts keyed by operation id, verification report, counters
  (`job_count`, `transitions`, `repair_rounds`, `review_rounds`, `plan_edits`)
  and a small result. No transcripts, clients, repository content or secrets.
- `jobs` and `findings` use `merge_receipts`: keyed, commutative,
  associative, idempotent; a less-settled outcome never overwrites a settled
  one. Counters use `operator.add` and are only incremented for new
  operations, so replay cannot double-count and resume never restores
  consumed allowance.
- Parallel branches write only keyed maps and counters, never shared scalars.

## Execution model

All executor methods are synchronous and run the graph on the calling thread
until it finishes, interrupts, parks, or reaches a requested pause at a
persisted super-step. There is no background thread, server, or scheduler; an
attempt only advances inside a host call, so it cannot outlive its owner.
Async hosts call `astart/aresume/adecide/acancel` (`asyncio.to_thread`);
calling a blocking method on an event-loop thread raises. Parallel research
branches run in LangGraph's own thread pool bounded by `max_concurrency`.

Graph limits (`policy.py`) only narrow: the effective value is the minimum
of package defaults, host admission and request settings (floors: one read
worker, ten transitions). Defaults — 2 parallel reads, 1 writer, 12 jobs,
2 repair rounds, 2 review rounds, 4 investigations, no delegation — are
conservative engineering choices, not measured optima.

## Dependencies

| Package | Declared range | Tested |
| --- | --- | --- |
| `langgraph` | `>=1.2.2,<1.3` | 1.2.12 (3.10, 3.12, 3.14); 1.2.2 (3.10, and 3.14 inside Locus's locked runtime) |
| `langgraph-checkpoint` | `>=4.1,<5` | 4.1.0, 4.2.0 |
| `langgraph-checkpoint-sqlite` | `>=3.1,<3.2` | 3.1.0, 3.1.1 |

**Conflict found and resolved:** every `langgraph>=1.2.3` requires
`langgraph-sdk>=0.4.2`, which pins `websockets<17`; Locus pins
`websockets==17.0`. `langgraph 1.2.2` (2026-05-26) is the newest release
compatible with Locus's lock (`langgraph-sdk 0.3.x` has no websockets
constraint). The package declares `>=1.2.2,<1.3`; resolving with Locus's
lock as constraints selects 1.2.2 without moving any Locus pin. The package
does not use `langgraph-sdk` (remote client) at all. When a later sdk lifts
the cap, the Locus-side pin can move without a package change.

Development and CI install `constraints.txt`, which pins exactly that
resolution (`langgraph 1.2.2`, `langgraph-checkpoint 4.2.0`,
`langgraph-checkpoint-sqlite 3.1.1`, `langgraph-sdk 0.3.15`), so code cannot
come to rely on a newer LangGraph API than Locus ships. The `latest` CI job
installs without constraints to show when a bump would be safe; bump
`constraints.txt` together with Locus's lock. None of the changes in
1.2.3–1.2.12 affect this package: they concern `DeltaChannel`, subgraphs,
v3/remote streaming, async cancellation, config callbacks/tags, per-node
trace policy and `interrupt(response_schema=...)`, none of which it uses.

`langgraph-checkpoint-sqlite` brings `sqlite-vec` (a loadable SQLite
extension shipped as a native library) even though only the checkpointer is
used; it must be signed like other bundled binaries. Official APIs used:
`StateGraph`, `START`/`END`, `Command(goto/resume)`, `Send`, `interrupt`,
`get_stream_writer`, `compile(checkpointer=)`, `stream(stream_mode=["custom",
"checkpoints"], durability="sync")`, `get_state`, `update_state`,
`SqliteSaver`, `JsonPlusSerializer`, `CipherProtocol`,
`langsmith.run_helpers.tracing_context`.
