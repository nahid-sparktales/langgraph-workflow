# Security and privacy

The host remains the trusted execution boundary. The package decides what to
attempt; the host decides what is allowed and does it.

## What workflow data can and cannot change

- Workflows are code-defined and selected by name from a fixed registry
  (`verified_change`, `research`). No workflow definition, module path,
  callable, or expression is ever loaded from data; there is no `eval`.
- Requests are validated at the boundary (`WorkflowRequest.from_dict`):
  unknown fields rejected, identifiers restricted to
  `[A-Za-z0-9][A-Za-z0-9._:@/-]{0,159}` without `..`, text ≤ 16,000 chars,
  JSON payloads ≤ 256 KB, ≤ 64 checks, ≤ 16 investigations, plans ≤ 32
  steps, limits non-negative integers.
- Model/job output can shape a plan or findings, but cannot change provider
  credentials, permission modes, edition identity, storage roots, tool
  allowlists, imports, or graph limits: those come from the host at
  admission and are frozen in state. Identity, permission and budget values
  found in graph state are never trusted for authorization — every decision
  goes back through `authorize_decision`, `revalidate` and the host's own job
  path.
- A read-only job cannot become a writer: `access` is fixed per node, and a
  read receipt reporting changed files blocks the attempt.

## Checkpoint deserialization

LangGraph's default `JsonPlusSerializer` is permissive: without
`LANGGRAPH_STRICT_MSGPACK`, loading a checkpoint can import and construct
arbitrary types. The store always uses
`JsonPlusSerializer(pickle_fallback=False, allowed_json_modules=None,
allowed_msgpack_modules=None)`: pickle is refused and only LangGraph's safe
type allowlist is reconstructed (unknown types load as plain data). This does
not depend on environment variables. Test:
`test_checkpoint_loading_never_constructs_arbitrary_types_or_unpickles`.

## Encryption

The host may inject a cipher (LangGraph `CipherProtocol`) from its own key
management; the package ships no cipher and no keys. With a cipher, every
checkpoint blob and pending write is encrypted by `StrictEncryptedSerializer`,
which — unlike upstream `EncryptedSerializer` — refuses plaintext blobs on
load. `require_encryption=True` without a cipher fails before any database
is opened. Tests use AES-GCM from `cryptography` (Locus already pins
`cryptography==50.0.0`), with the key held only in test memory.

What stays plaintext even with a cipher, by design and verified with a
planted secret:

| Location | Content |
| --- | --- |
| `checkpoints.thread_id`, `checkpoint_ns`, ids | opaque attempt id and LangGraph ids |
| `checkpoints.metadata` (JSON) | LangGraph step/source/parents plus `configurable` values — the executor puts only `thread_id` there |
| `writes.channel`, `task_path` | channel/node names |
| `lgw_*` tables | attempt/run ids, versions, request fingerprint, status, lease owner (`host:pid:nonce`), decision ids and response digests |

Without a cipher, state blobs (request goal, plan, receipts) are plaintext
in the sidecar file and its WAL. `test_encrypted_store_has_no_plaintext_secret_including_wal`
scans the database, `-wal` and `-shm` files for the planted secret;
`test_planted_secret_absent_from_events_metadata_and_sidecar` checks events,
metadata and sidecar tables. The sidecar file is created `0600` in a
directory created `0700`; hosts should place it in their private profile
area. Backups/exports of the sidecar are the host's responsibility and carry
whatever the table above says is plaintext.

## Events and diagnostics

Nodes emit typed events; `redact()` removes values under secret-looking keys
(`api_key`, `authorization`, `token`, `password`, `secret`, …) and secret
patterns (`sk-…`, `ghp_…`, `xox?-…`, `AKIA…`, `Bearer …`, PEM private keys),
and bounds depth, item count and string length. Events never include graph
state, checkpoints, transcripts or raw tool output. `workflow.phase` events
(internal node names) are marked `diagnostic` and are dropped unless the host
constructs the executor with `diagnostics=True`. Hosts should still apply
their own redaction (Locus's `sanitize_event` runs on every appended event).

## Outbound traffic

The package makes no network calls. LangSmith tracing is suppressed for
every graph run with the context-local `tracing_context(enabled=False)`,
which overrides ambient `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2` without
modifying the environment. `test_langsmith_tracing_is_suppressed_despite_ambient_env`
runs a workflow with tracing variables pointing at a local collector and
asserts zero requests, and runs a control that bypasses the executor to prove
the collector would receive traces. Optional external tracing is not
supported in this release. The library never configures logging or changes
environment variables (`test_library_does_not_touch_environment_or_logging`).

## Paths

The package never resolves workspace paths; hosts do. The fixture host's
`safe_path` refuses absolute paths, `..` and symlink escapes
(`test_symlink_escape_is_refused_by_host_path_logic`); Locus's
`task_state.relative_path`, `fingerprints` (no symlink following) and tool
path checks apply in the Locus adapter.

## Dependencies

Declared with bounded ranges; releases must consume a hash-verified lock
(Locus: `requirements-runtime.lock`, `--require-hashes --only-binary=:all:`).
No Git-branch dependencies. Adding the package to Locus's runtime adds 24
distributions (listed in `integrations/locus/compatibility.json`) with 8
native binaries; all were scanned with Locus's wallet-edition audit
patterns with zero matches. `pip-audit` was not run in this phase.
