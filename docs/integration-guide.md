# Integration guide

## Implementing a host

Implement the nine methods of `langgraph_workflow.WorkflowHost` over your
runtime's existing job, permission, verification and event machinery, then
run the shared contract:

```python
from langgraph_workflow.testing import run_host_contract
results = run_host_contract(host, request, count_events=lambda event_id: ...)
assert all(v == "pass" for v in results.values())
```

Drive workflows with `WorkflowExecutor(host, store_path)`; choose
`store_path` inside the runtime's private profile. The Locus reference
adapter (`integrations/locus/adapter_reference.py`) is a complete example.

## Local development against Locus

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
python3 -m build
integrations/locus/prepare_checkout.sh /path/to/locus <sha> /tmp/locus-it "$PWD/dist/langgraph_workflow-0.1.0-py3-none-any.whl"
LOCUS_PYTHON=/tmp/locus-it/venv/bin/python .venv/bin/python -m pytest -q tests/integration
```

`prepare_checkout.sh` only reads the Locus repository (`git archive`), builds
a disposable checkout with each patch as a separate commit, installs Locus's
hash-locked runtime exactly as the app build does, and installs the wheel
constrained to those pins. During development an editable install
(`pip install -e`) into a Locus dev venv is fine; never point a packaged app
at a sibling checkout or a home directory.

## Consuming a release in Locus (Prompt 2)

1. Add `langgraph-workflow==<version>` to `agent/requirements-runtime.in`
   and regenerate `requirements-runtime.lock` with
   `pip-compile --generate-hashes` under Python 3.14. With Locus's current
   `websockets==17.0` the resolver selects `langgraph==1.2.2`. The wheel must
   come from a reviewed immutable artifact (internal index or vendored
   wheel with its hash); do not publish or depend on a Git URL.
2. Keep it out of `agent/pyproject.toml` core dependencies (or put it in an
   optional extra) so ordinary dev installs and native startup do not need it.
3. Stage the added native libraries (orjson, ormsgpack, xxhash, zstandard,
   uuid_utils, sqlite-vec `vec0.dylib`, pyyaml, …) through the existing
   signing/notarization path; run `Tools/AuditAppEdition.py` on the bundle.
4. Move the adapter into a Locus feature module; resolve it through
   `api/dependencies.py`; do not add routes or globals to `server.py` or
   state to `AppModel`.
5. Apply patch 0001 (or its reviewed equivalent) as a separate refactor
   commit before the behavior change. Extract a writer-slice seam from
   `server._run_team_writer` in the same way.

## Feature-off behavior

Nothing in Locus imports the package today. In Prompt 2 the import must be
lazy and guarded: when the package is absent or its `CONTRACT_VERSION`
differs, report the workflow executor as unavailable with the reason, keep
native chat/teams/goals/capsules unchanged, and never auto-route work to it.
The feature stays off by default. With the package installed but the feature
off, the full Locus backend suite passes unchanged (see
`integrations/locus/compatibility.json`).

## Storage, migration and rollback

- Sidecar location: `<Locus profile>/langgraph-workflow/checkpoints.sqlite3`
  (edition- and profile-scoped like other Locus stores). No Locus schema
  changes; links to Locus runs/tasks/job attempts are by id.
- Package upgrades that change a workflow's `DEFINITION_VERSION`,
  `STATE_SCHEMA_VERSION` or `CONTRACT_VERSION` make older attempts raise
  `IncompatibleAttempt` rather than run under a new graph. Keep the previous
  package version available to finish or inspect them, or start a new
  attempt.
- Rollback = disable new admissions and uninstall/pin back. Never delete the
  sidecar while attempts are active or paused; `prune()` only removes
  terminal attempts. Never replay a workflow's work under the native executor.

## Later native UI wiring (Prompt 2)

Translate `workflow_event` payloads (schema `langgraph-workflow.event/1`)
into existing run-history/task-detail surfaces through the adapter; Swift
should never decode LangGraph internals. Map statuses per the table in
`docs/locus-integration-map.md`. Reuse existing approval, recovery and
acceptance controls; add protocol fixtures and decoding tests for any new
field.
