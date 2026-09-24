"""Graph-owned sidecar storage: LangGraph's SQLite checkpointer plus a few
package tables for attempt identity, ownership leases, cooperative controls
and consumed decisions.

The host chooses the file location; nothing defaults to a user profile. The
checkpointer does not enforce workflow ownership, so :meth:`lease` provides a
cross-process lease in the same database (``BEGIN IMMEDIATE``), which works
across processes on one machine. Hosts with their own admission leases should
hold those as well.

Plaintext exposure (see docs/security.md): checkpoint and pending-write blobs
go through the serializer and are encrypted when a cipher is injected. Thread
ids, checkpoint metadata (JSON), channel names and the package tables are
plaintext, so only opaque identifiers may be placed in them.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.serde.base import CipherProtocol, SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

_SIDE_TABLES = """
CREATE TABLE IF NOT EXISTS lgw_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    workflow TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    contract_version TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lgw_leases (
    attempt_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    pid INTEGER NOT NULL,
    hostname TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lgw_controls (
    attempt_id TEXT PRIMARY KEY,
    pause INTEGER NOT NULL DEFAULT 0,
    cancel INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lgw_decisions (
    decision_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    response_digest TEXT NOT NULL,
    consumed_at REAL NOT NULL
);
"""


class AttemptBusy(RuntimeError):
    """Another live executor owns this attempt."""


class EncryptionRequired(RuntimeError):
    """Encryption was required but no cipher, or a plaintext blob, was found."""


class StrictEncryptedSerializer(SerializerProtocol):
    """Encrypt every blob with a host-injected cipher and refuse plaintext.

    Upstream ``EncryptedSerializer`` silently accepts unencrypted blobs on
    load; this wrapper fails closed instead.
    """

    def __init__(self, cipher: CipherProtocol, serde: SerializerProtocol) -> None:
        self.cipher = cipher
        self.serde = serde

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        kind, data = self.serde.dumps_typed(obj)
        name, ciphertext = self.cipher.encrypt(data)
        return f"{kind}+{name}", ciphertext

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        kind, blob = data
        if "+" not in kind:
            raise EncryptionRequired("refusing to load a plaintext checkpoint blob")
        kind, name = kind.split("+", 1)
        return self.serde.loads_typed((kind, self.cipher.decrypt(name, blob)))


def strict_serializer(cipher: CipherProtocol | None = None) -> SerializerProtocol:
    # No pickle fallback, and msgpack restricted to LangGraph's safe-type
    # allowlist: loading a checkpoint can never import or call arbitrary code.
    base = JsonPlusSerializer(
        pickle_fallback=False, allowed_json_modules=None, allowed_msgpack_modules=None
    )
    return base if cipher is None else StrictEncryptedSerializer(cipher, base)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class CheckpointStore:
    """Constructing a store performs no I/O; :meth:`open` does."""

    def __init__(
        self,
        path: str | os.PathLike,
        *,
        cipher: CipherProtocol | None = None,
        require_encryption: bool = False,
    ) -> None:
        self.path = Path(path)
        self.cipher = cipher
        self.require_encryption = require_encryption
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self._conn: sqlite3.Connection | None = None
        self._saver: SqliteSaver | None = None

    # -- lifecycle -------------------------------------------------------------
    @property
    def saver(self) -> SqliteSaver:
        if self._saver is None:
            self.open()
        assert self._saver is not None
        return self._saver

    def open(self) -> None:
        if self._saver is not None:
            return
        if self.require_encryption and self.cipher is None:
            raise EncryptionRequired("checkpoint encryption is required but no cipher was given")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        # WAL + FULL: a committed super-step survives process and power loss.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        saver = SqliteSaver(conn, serde=strict_serializer(self.cipher))
        saver.setup()
        with self._side() as side:
            side.executescript(_SIDE_TABLES)
        self._conn, self._saver = conn, saver

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = self._saver = None

    @contextmanager
    def _side(self) -> Iterator[sqlite3.Connection]:
        # Short-lived connections so pause/cancel/status work from any thread
        # or process while another thread is executing the graph.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    def _ensure_open(self) -> None:
        if self._saver is None:
            self.open()

    # -- attempts --------------------------------------------------------------
    def register(self, request: Any, versions: dict) -> dict | None:
        """Insert the attempt row, or return the existing one unchanged."""
        self._ensure_open()
        now = time.time()
        with self._side() as side:
            side.execute("BEGIN IMMEDIATE")
            row = side.execute(
                "SELECT * FROM lgw_attempts WHERE attempt_id=?", (request.attempt_id,)
            ).fetchone()
            if row is None:
                side.execute(
                    "INSERT INTO lgw_attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.attempt_id, request.run_id, request.workflow,
                        versions["definition"], versions["schema"], versions["contract"],
                        request.fingerprint, "running", now, now,
                    ),
                )
            side.execute("COMMIT")
        return dict(row) if row is not None else None

    def attempt(self, attempt_id: str) -> dict | None:
        self._ensure_open()
        with self._side() as side:
            row = side.execute(
                "SELECT * FROM lgw_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_status(self, attempt_id: str, status: str) -> None:
        with self._side() as side:
            side.execute(
                "UPDATE lgw_attempts SET status=?, updated_at=? WHERE attempt_id=?",
                (status, time.time(), attempt_id),
            )

    # -- ownership -------------------------------------------------------------
    @contextmanager
    def lease(self, attempt_id: str, seconds: float = 120.0) -> Iterator[None]:
        self._ensure_open()
        self._acquire(attempt_id, seconds)
        try:
            yield
        finally:
            with self._side() as side:
                side.execute(
                    "DELETE FROM lgw_leases WHERE attempt_id=? AND owner=?",
                    (attempt_id, self.owner),
                )

    def _acquire(self, attempt_id: str, seconds: float) -> None:
        host, now = socket.gethostname(), time.time()
        with self._side() as side:
            side.execute("BEGIN IMMEDIATE")
            row = side.execute(
                "SELECT * FROM lgw_leases WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is not None and row["owner"] != self.owner and row["expires_at"] > now:
                # Same machine: a dead process no longer owns anything.
                if row["hostname"] != host or _pid_alive(row["pid"]):
                    side.execute("ROLLBACK")
                    raise AttemptBusy(f"attempt {attempt_id} is owned by another executor")
            side.execute(
                "INSERT OR REPLACE INTO lgw_leases VALUES (?,?,?,?,?)",
                (attempt_id, self.owner, os.getpid(), host, now + seconds),
            )
            side.execute("COMMIT")

    def renew(self, attempt_id: str, seconds: float = 120.0) -> None:
        with self._side() as side:
            side.execute(
                "UPDATE lgw_leases SET expires_at=? WHERE attempt_id=? AND owner=?",
                (time.time() + seconds, attempt_id, self.owner),
            )

    def lease_holder(self, attempt_id: str) -> str | None:
        with self._side() as side:
            row = side.execute(
                "SELECT owner, pid, hostname, expires_at FROM lgw_leases WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        if row["hostname"] == socket.gethostname() and not _pid_alive(row["pid"]):
            return None
        return row["owner"]

    # -- cooperative controls --------------------------------------------------
    def request(self, attempt_id: str, control: str, value: bool = True) -> None:
        assert control in ("pause", "cancel")
        self._ensure_open()
        with self._side() as side:
            side.execute(
                "INSERT INTO lgw_controls(attempt_id) VALUES (?) ON CONFLICT DO NOTHING",
                (attempt_id,),
            )
            side.execute(
                f"UPDATE lgw_controls SET {control}=? WHERE attempt_id=?",
                (int(value), attempt_id),
            )

    def controls(self, attempt_id: str) -> dict[str, bool]:
        with self._side() as side:
            row = side.execute(
                "SELECT pause, cancel FROM lgw_controls WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        return {"pause": bool(row and row["pause"]), "cancel": bool(row and row["cancel"])}

    # -- decisions -------------------------------------------------------------
    def consume_decision(self, decision_id: str, attempt_id: str, response_digest: str) -> bool:
        """Record a decision once. True if new or an identical re-delivery."""
        with self._side() as side:
            side.execute("BEGIN IMMEDIATE")
            row = side.execute(
                "SELECT response_digest FROM lgw_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if row is None:
                side.execute(
                    "INSERT INTO lgw_decisions VALUES (?,?,?,?)",
                    (decision_id, attempt_id, response_digest, time.time()),
                )
            side.execute("COMMIT")
        return row is None or row["response_digest"] == response_digest

    def decision_consumed(self, decision_id: str) -> bool:
        with self._side() as side:
            return side.execute(
                "SELECT 1 FROM lgw_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone() is not None

    # -- retention -------------------------------------------------------------
    def prune(self, *, older_than_seconds: float, terminal: tuple[str, ...]) -> list[str]:
        """Delete checkpoint history of terminal, unleased attempts only.

        Active, paused and waiting attempts keep their full history. Host
        records (tasks, receipts, usage) are never touched.
        """
        self._ensure_open()
        cutoff = time.time() - older_than_seconds
        marks = ",".join("?" * len(terminal))
        with self._side() as side:
            rows = side.execute(
                f"SELECT attempt_id FROM lgw_attempts WHERE status IN ({marks}) AND updated_at<?",
                (*terminal, cutoff),
            ).fetchall()
        pruned = []
        for row in rows:
            attempt_id = row["attempt_id"]
            if self.lease_holder(attempt_id) is not None:
                continue
            self.saver.delete_thread(attempt_id)
            with self._side() as side:
                side.execute("UPDATE lgw_attempts SET status='pruned' WHERE attempt_id=?",
                             (attempt_id,))
            pruned.append(attempt_id)
        return pruned
