# Locus integration harness

Everything here is **integration-harness code**, not a production dependency
and not part of the installed package. It proves the Locus boundary at a
recorded revision; Prompt 2 moves a reviewed adapter into Locus itself.

| File | Purpose |
| --- | --- |
| `adapter_reference.py` | `LocusHost`, a `WorkflowHost` over real Locus services (`run_read_job`, `AgentCore.run_turn`, `TaskVerifier`, `RunStore`) |
| `patches/0001-expose-bounded-read-job-seam.patch` | The only extraction: public `TeamOrchestrator.run_read_job` delegating to `_call_agent`, plus a Locus test. Pure refactor. |
| `prepare_checkout.sh` | Builds a disposable checkout (`git archive` of a SHA → new repo, one commit per patch) and a venv with Locus's hash-locked runtime plus the wheel, constrained to Locus's pins |
| `harness.py` | One scenario step per process against a temporary `OLLAMA_CODE_HOME`; scripted provider clients are the only fake |
| `compatibility.json` | Tested SHAs, versions, commands and honest results |

## Reproduce

```bash
python3 -m build
integrations/locus/prepare_checkout.sh ../locus 5ac5b5b1c450eff0013ed6caae8c696dfc6fb0eb /tmp/locus-it \
    "$PWD/dist/langgraph_workflow-0.1.0-py3-none-any.whl"
LOCUS_PYTHON=/tmp/locus-it/venv/bin/python .venv/bin/python -m pytest -q tests/integration/test_locus.py
(cd /tmp/locus-it/checkout && ../venv/bin/python -m pytest -q)   # Locus suite, patched
```

The source repository is only read. Nothing is pushed, published, installed
into the Locus app, or written to a real Locus profile.

## Scenarios (`tests/integration/test_locus.py`)

- `change-start` / `change-decide`: plan approval interrupt, process restart,
  forged-actor rejection, real permission request and decider, TaskVerifier
  receipts, no repeated read jobs, event dedupe and cursor replay.
- `change-deny-writes`: decider denies `write_file` → `needs_review/job_denied`, no file.
- `stale-final`: file changes after verification → Locus `completion()` detects
  it → re-verification fails → `needs_review/final_checks_failed`.
- `research`: two parallel read-only investigations + synthesis through the
  scheduler lease and usage ledger; verified by a real check.
- `unpatched`: without patch 0001 the adapter withholds `jobs.read`; workflows
  needing reads wait for the capability, a supplied plan still works.
- `contract`: the 17-check shared host contract against `LocusHost`.

## Known gaps before production (Prompt 2)

The writer path calls `AgentCore.run_turn` directly instead of a reviewed
single-writer-slice seam (no scheduler `writer_slot`, goal attachment, or
writer-route install); decisions use a callable instead of `ChatService`
decisions; run admission bypasses the queue/supervisor; `workflow_event` rows
have no native presentation yet; and there is no cross-process per-checkout
writer lock in Locus Python to rely on.
