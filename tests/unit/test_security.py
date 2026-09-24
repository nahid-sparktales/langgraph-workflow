"""Privacy and security: planted secrets, encryption, deserialization, egress."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from conftest import CHECK, change_request, respond
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from langgraph_workflow import EncryptionRequired, WorkflowExecutor
from langgraph_workflow.checkpoints import StrictEncryptedSerializer, strict_serializer
from langgraph_workflow.testing import FixtureHost

SECRET = "sk-live-PLANTEDSECRET0123456789"
SECRET_PLAN = {"steps": [{"title": f"Use key {SECRET} to write"}],
               "api_key": SECRET}


class FixtureCipher:
    """Test-only AES-GCM cipher. Production keys come from the host keystore."""

    def __init__(self, key: bytes | None = None) -> None:
        self.aead = AESGCM(key or AESGCM.generate_key(bit_length=256))

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(12)
        return "aesgcm", nonce + self.aead.encrypt(nonce, plaintext, None)

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        assert ciphername == "aesgcm"
        return self.aead.decrypt(ciphertext[:12], ciphertext[12:], None)


def _store_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.glob("checkpoints.sqlite3*"))


def _run_with_secret(root, cipher=None, diagnostics=False):
    host = FixtureHost(root, {"plan_approval": True,
                              "writes": {"implement": {"result.txt": "done\n"}}})
    ex = WorkflowExecutor(host, root / "checkpoints.sqlite3", cipher=cipher,
                          diagnostics=diagnostics)
    waiting = ex.start(change_request(goal=f"Deploy with token {SECRET}", plan=SECRET_PLAN))
    status = ex.decide(respond(waiting))
    ex.close()
    return host, status


def test_planted_secret_absent_from_events_metadata_and_sidecar(tmp_path):
    host, status = _run_with_secret(tmp_path)
    assert status.status == "verified"
    events = json.dumps(host.events_after(0, 1000))
    assert SECRET not in events
    assert "[redacted]" in events  # the plan summary in decision.pending was scrubbed
    with sqlite3.connect(tmp_path / "checkpoints.sqlite3") as db:
        metadata = json.dumps(db.execute("SELECT metadata FROM checkpoints").fetchall(),
                              default=str)
        side = json.dumps([db.execute(f"SELECT * FROM {t}").fetchall() for t in
                           ("lgw_attempts", "lgw_leases", "lgw_controls", "lgw_decisions")])
    assert SECRET not in metadata and SECRET not in side
    # Without a cipher, state blobs are plaintext by design (documented).
    assert SECRET.encode() in _store_bytes(tmp_path)


def test_encrypted_store_has_no_plaintext_secret_including_wal(tmp_path):
    cipher = FixtureCipher()
    host, status = _run_with_secret(tmp_path, cipher=cipher)
    assert status.status == "verified"
    assert SECRET.encode() not in _store_bytes(tmp_path)
    reopened = WorkflowExecutor(host, tmp_path / "checkpoints.sqlite3", cipher=cipher)
    assert reopened.status("att-1").status == "verified"  # decrypts with the host key
    reopened.close()


def test_encryption_fails_closed(tmp_path):
    host = FixtureHost(tmp_path)
    with pytest.raises(EncryptionRequired):
        WorkflowExecutor(host, tmp_path / "c.sqlite3", require_encryption=True).start(
            change_request())
    strict = StrictEncryptedSerializer(FixtureCipher(), strict_serializer())
    plaintext = strict_serializer().dumps_typed({"a": 1})
    with pytest.raises(EncryptionRequired):
        strict.loads_typed(plaintext)
    from cryptography.exceptions import InvalidTag
    with pytest.raises(InvalidTag):  # a different key cannot read, or forge, a blob
        StrictEncryptedSerializer(FixtureCipher(), strict_serializer()).loads_typed(
            StrictEncryptedSerializer(FixtureCipher(), strict_serializer()).dumps_typed({"a": 1}))


@dataclass
class Gadget:
    x: int = 1

    def __post_init__(self):
        CONSTRUCTED.append(self)


CONSTRUCTED: list = []


def test_checkpoint_loading_never_constructs_arbitrary_types_or_unpickles():
    blob = JsonPlusSerializer().dumps_typed(Gadget())
    CONSTRUCTED.clear()
    assert strict_serializer().loads_typed(blob) == {"x": 1}
    assert CONSTRUCTED == []
    import pickle
    with pytest.raises(NotImplementedError):
        strict_serializer().loads_typed(("pickle", pickle.dumps(Gadget())))


def test_diagnostic_events_are_opt_in(tmp_path):
    host, _ = _run_with_secret(tmp_path / "a")
    assert not any(e["diagnostic"] for e in host.events_after(0, 1000))
    host, _ = _run_with_secret(tmp_path / "b", diagnostics=True)
    diagnostic = [e for e in host.events_after(0, 1000) if e["diagnostic"]]
    assert diagnostic and SECRET not in json.dumps(diagnostic)


def test_symlink_escape_is_refused_by_host_path_logic(tmp_path):
    host = FixtureHost(tmp_path, {"writes": {"implement": {"result.txt": "done\n"}}})
    outside = tmp_path / "outside.txt"
    outside.write_text("done")
    (host.workspace / "link.txt").symlink_to(outside)
    ex = WorkflowExecutor(host, tmp_path / "c.sqlite3")
    status = ex.start(change_request(checks=[CHECK, {"id": "link", "kind": "file_exists",
                                                     "path": "link.txt", "requirement": "x"}]))
    assert (status.status, status.blocker) == ("needs_review", "check_denied")
    ex.close()


def test_library_does_not_touch_environment_or_logging(tmp_path):
    import logging
    env, handlers = dict(os.environ), list(logging.getLogger().handlers)
    _run_with_secret(tmp_path)
    assert dict(os.environ) == env and logging.getLogger().handlers == handlers


_TRACE_SCRIPT = r"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, {tests!r})
from conftest import change_request
from langgraph_workflow import WorkflowExecutor
from langgraph_workflow.testing import FixtureHost
from langchain_core.tracers.langchain import wait_for_all_tracers
root = Path(tempfile.mkdtemp())
host = FixtureHost(root, {{"writes": {{"implement": {{"result.txt": "done\n"}}}}}})
ex = WorkflowExecutor(host, root / "c.sqlite3")
if sys.argv[1] == "executor":
    assert ex.start(change_request()).status == "verified"
else:  # control: the same compiled graph invoked without the executor's guard
    from langgraph_workflow.contracts import WorkflowRequest
    graph = ex._graph("verified_change")
    graph.invoke({{"request": WorkflowRequest.from_dict(change_request()).to_dict(),
                   "limits": {{"max_transitions": 60, "max_jobs": 12, "max_plan_steps": 16,
                              "max_repair_rounds": 2, "max_review_rounds": 2,
                              "max_plan_edits": 3, "max_parallel_reads": 2}}}},
                 {{"configurable": {{"thread_id": "control"}}}})
wait_for_all_tracers()
"""


def test_langsmith_tracing_is_suppressed_despite_ambient_env(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(self.path)
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_PATCH = do_POST  # noqa: N815

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {**os.environ, "LANGSMITH_TRACING": "true", "LANGCHAIN_TRACING_V2": "true",
           "LANGSMITH_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
           "LANGSMITH_API_KEY": "lsv2_pt_fake", "LANGSMITH_PROJECT": "egress-test"}
    script = tmp_path / "trace.py"
    script.write_text(_TRACE_SCRIPT.format(tests=str(Path(__file__).parents[1])))
    try:
        subprocess.run([sys.executable, str(script), "executor"], env=env, check=True,
                       timeout=120, capture_output=True)
        executor_requests = len(received)
        subprocess.run([sys.executable, str(script), "control"], env=env, check=True,
                       timeout=120, capture_output=True)
    finally:
        server.shutdown()
    assert len(received) > executor_requests, "control run did not trace; test is inconclusive"
    assert executor_requests == 0
