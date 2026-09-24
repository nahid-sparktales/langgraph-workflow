# Evaluation

Five separate questions, answered by different evidence. Only the first
three are answered in this phase.

| Question | Evidence in this repository | Status |
| --- | --- | --- |
| Package correctness | `tests/unit`, `tests/contract`, `tests/recovery` on the fixture host | Answered |
| Locus adapter compatibility | `tests/integration/test_locus.py` against Locus 5ac5b5b1 + patch 0001; Locus backend suite with and without the patch | Answered for the recorded revision; see `integrations/locus/compatibility.json` |
| Recovery and safety reliability | Process-kill crash windows, ownership, decisions, cancellation, privacy tests | Answered with fixtures and one real-Locus restart path |
| Orchestration-only overhead | `examples/evaluate.py` → `evaluation/fixture-overhead.json` | Measured with fixtures (below) |
| Live task quality / time / usage vs native Locus | none | **Not run.** No paid or external-action campaign was authorized |

## Fixture overhead

`python examples/evaluate.py --runs 20` runs each scenario 21 times in a
fresh store (first run discarded as warm-up) with a fixture host whose jobs
take ~0 ms, so the numbers are graph setup, SQLite checkpointing
(`durability="sync"`, `synchronous=FULL`), event publication and fixture
SQLite I/O. Raw per-run rows (status, timings, checkpoint and pending-write
counts, database bytes, host-call counts) are in
`evaluation/fixture-overhead.json`.

RESULTS_TABLE

What this does and does not show:

- It bounds the package's own cost per workflow at tens of milliseconds on
  this machine; real jobs (model calls, tools, checks) dominate by orders of
  magnitude.
- Host-call counts are exact for these scenarios: e.g. verified change with a
  supplied plan makes one `execute` and two `verify` calls; with the Locus
  adapter the second (final) verification is answered from Locus's
  `completion()` without re-running checks when evidence is provably current.
- It does not establish model cost savings, quality, or wall-clock parity
  with native Locus teams. One machine, warm caches, other activity present.

## Future live comparison (requires separate authorization)

Run native Locus and the LangGraph executor on the same task set with fresh,
matched workspaces from the same baseline checkout, the same account/model
route, context, tools, required checks, reviewer policy and budget.
Interleave conditions, keep every started attempt in the denominator
(including failures, timeouts and incomplete results), and record: total wall
time, model calls and tokens per stage from Locus's ledgers (with coverage
and unknowns), tool calls, retries, extra agents, verification coverage,
recovery behavior, and correctness against held-out checks. Use Locus's
existing campaign caps (per-scenario spend/call/time limits) and stop on
missing pricing or unsettled usage. Fixture timing must not be cited as live
evidence.
