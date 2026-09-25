"""A durable host whose jobs are performed by an external agent.

Used when the executor runs outside the agent runtime (for example as an MCP
server inside a Locus plugin): the runtime's agent performs each job with its
own tools and permissions and reports back. The host never runs models,
tools, or shell commands itself. It

- hands out each operation once and records it before it is shown to the agent;
- computes changed files by diffing workspace snapshots taken at dispatch
  and at report, so a read job that edited files is caught regardless of
  what the report claims;
- verifies file/JSON checks itself against the workspace (command checks
  are reported as unsupported, which ends in ``needs_review``);
- accepts decisions only from its configured actor, which the caller must
  bind to a real human channel (the MCP server uses elicitation).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts import (
    Admission,
    CheckResult,
    ContractError,
    DecisionResponse,
    JobReceipt,
    JobSpec,
    VerificationReport,
    WorkflowRequest,
    digest,
    small_json,
    text,
)
from .events import WorkflowEvent
from .filechecks import changed, check_file, snapshot
from .ports import CAPABILITIES

OUTCOMES = {"completed": "settled", "failed": "failed", "refused": "denied"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops (
    operation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, spec TEXT NOT NULL,
    fingerprint TEXT NOT NULL, status TEXT NOT NULL, before TEXT, receipt TEXT,
    dispatched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, attempt_id TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS cancels (attempt_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS claims (
    operation_id TEXT PRIMARY KEY, token TEXT NOT NULL, dispatched_at REAL NOT NULL,
    delivered_at REAL);
"""


class ReportRejected(ValueError):
    pass


class AgentHost:
    def __init__(self, data_dir: str | Path, workspace: str | Path, *,
                 actor: str = "user", policy: Callable[[], dict] | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.db_path = Path(data_dir) / "agent-host.sqlite3"
        self.actor = actor
        # Current user settings: approval policy and limits for new attempts.
        self.policy = policy or dict
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    # -- admission ---------------------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        return CAPABILITIES

    def policy_ref(self) -> str:
        return digest({"host": "agent/1", "workspace": str(self.workspace)})

    def admit(self, request: WorkflowRequest) -> Admission:
        if not self.workspace.is_dir():
            return Admission(False, reason="workspace_missing")
        policy = self.policy()
        return Admission(True, policy_ref=self.policy_ref(),
                         plan_approval=bool(policy.get("plan_approval", True)),
                         conflict_approval=bool(policy.get("conflict_approval", True)),
                         limits=dict(policy.get("limits", {})))

    def revalidate(self, request: WorkflowRequest, policy_ref: str) -> Admission:
        if policy_ref != self.policy_ref():
            return Admission(False, reason="checkout_changed")
        return self.admit(request)

    def authorize_decision(self, response: DecisionResponse) -> bool:
        return response.actor == self.actor

    # -- jobs --------------------------------------------------------------------
    def execute(self, spec: JobSpec) -> JobReceipt:
        existing = self.lookup(spec.operation_id)
        if existing is not None:
            if existing.input_fingerprint != spec.input_fingerprint:
                return JobReceipt(spec.operation_id, existing.job_id, "uncertain",
                                  spec.input_fingerprint, detail="operation_conflict")
            return existing  # handed out once; never re-dispatched
        with self._db() as db:
            if db.execute("SELECT 1 FROM cancels WHERE attempt_id=?",
                          (spec.attempt_id,)).fetchone():
                return JobReceipt(spec.operation_id, "", "cancelled", spec.input_fingerprint,
                                  detail="attempt cancelled")
            db.execute("INSERT OR IGNORE INTO ops VALUES (?,?,?,?,?,?,?,?)",
                       (spec.operation_id, spec.attempt_id, json.dumps(spec.to_dict()),
                        spec.input_fingerprint, "running",
                        json.dumps(snapshot(self.workspace)), None, time.time()))
        return self.lookup(spec.operation_id)

    def lookup(self, operation_id: str) -> JobReceipt | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM ops WHERE operation_id=?",
                             (operation_id,)).fetchone()
        if row is None:
            return None
        if row["receipt"]:
            return JobReceipt.from_dict(json.loads(row["receipt"]))
        return JobReceipt(operation_id, _job_id(operation_id), row["status"],
                          row["fingerprint"], detail="waiting for the agent's report")

    def pending(self, attempt_id: str) -> list[dict]:
        """Jobs handed to the agent that have not been reported."""
        with self._db() as db:
            rows = db.execute("SELECT spec FROM ops WHERE attempt_id=? AND receipt IS NULL "
                              "ORDER BY dispatched_at", (attempt_id,)).fetchall()
        return [json.loads(r["spec"]) for r in rows]

    def dispatch(self, operation_id: str) -> dict:
        """Hand an assigned job to its agent: the claim the report must carry.

        A job with an ``assignee`` is shown only through this call, so an
        agent in some other chat cannot report it. Calling it again returns
        the same claim (the hand-off may be retried).
        """
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT spec, receipt FROM ops WHERE operation_id=?",
                             (operation_id,)).fetchone()
            if row is None or row["receipt"]:
                db.execute("ROLLBACK")
                raise ReportRejected("no pending job with that id")
            spec = json.loads(row["spec"])
            if not spec.get("assignee"):
                db.execute("ROLLBACK")
                raise ReportRejected("this job is not assigned to a particular agent")
            db.execute("INSERT OR IGNORE INTO claims VALUES (?,?,?,NULL)",
                       (operation_id, secrets.token_urlsafe(18), time.time()))
            claim = db.execute("SELECT token, delivered_at FROM claims WHERE operation_id=?",
                               (operation_id,)).fetchone()
            db.execute("COMMIT")
        return {"claim": claim["token"], "spec": spec,
                "delivered": claim["delivered_at"] is not None}

    def mark_delivered(self, operation_id: str) -> bool:
        """Record that the runtime delivered the hand-off to the agent's chat."""
        with self._db() as db:
            return db.execute("UPDATE claims SET delivered_at=? WHERE operation_id=? AND "
                              "delivered_at IS NULL", (time.time(), operation_id)).rowcount > 0

    def report(self, operation_id: str, outcome: str, result: Any, note: str = "",
               claim: str = "") -> JobReceipt:
        """Record the agent's outcome for a dispatched job, exactly once."""
        if outcome not in OUTCOMES:
            raise ReportRejected(f"outcome must be one of {', '.join(OUTCOMES)}")
        try:
            output = small_json(result if result is not None else {}, "result")
            note = text(note, "note", 2000)
        except ContractError as error:
            raise ReportRejected(str(error)) from error
        if not isinstance(output, dict):
            raise ReportRejected("result must be an object")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM ops WHERE operation_id=?",
                             (operation_id,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise ReportRejected("unknown operation")
            if row["receipt"]:
                db.execute("ROLLBACK")
                raise ReportRejected("operation already reported")
            assignee = json.loads(row["spec"]).get("assignee")
            if assignee:
                held = db.execute("SELECT token FROM claims WHERE operation_id=?",
                                  (operation_id,)).fetchone()
                if held is None or not secrets.compare_digest(held["token"], claim or ""):
                    db.execute("ROLLBACK")
                    raise ReportRejected("this job belongs to another agent; only the chat it "
                                         "was handed to can report it")
            files = changed(json.loads(row["before"]) if row["before"] else None,
                            snapshot(self.workspace))
            receipt = JobReceipt(
                operation_id, _job_id(operation_id), OUTCOMES[outcome], row["fingerprint"],
                output=output,
                # Unknown changes (snapshot limit) count as changes: they are
                # never taken as proof that a read job left files alone.
                changed_files=tuple(files) if files is not None else ("<unobserved>",),
                usage={"coverage": "unknown", "source": "agent"},
                detail=note or outcome,
            )
            db.execute("UPDATE ops SET status=?, receipt=? WHERE operation_id=?",
                       (receipt.status, json.dumps(receipt.to_dict()), operation_id))
            db.execute("COMMIT")
        return receipt

    def cancel(self, attempt_id: str) -> bool:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO cancels VALUES (?)", (attempt_id,))
            for row in db.execute("SELECT operation_id, fingerprint FROM ops WHERE "
                                  "attempt_id=? AND receipt IS NULL", (attempt_id,)).fetchall():
                receipt = JobReceipt(row["operation_id"], _job_id(row["operation_id"]),
                                     "cancelled", row["fingerprint"],
                                     detail="cancelled; the agent must stop this job")
                db.execute("UPDATE ops SET status='cancelled', receipt=? WHERE operation_id=?",
                           (json.dumps(receipt.to_dict()), row["operation_id"]))
        # The agent's jobs run inside its own tool calls; once cancel is
        # called (itself a tool call) no job is executing for this attempt.
        return True

    # -- verification ------------------------------------------------------------
    def verify(self, request: WorkflowRequest, checks: tuple, *, final: bool) -> VerificationReport:
        results = []
        for check in checks:
            state, detail = check_file(self.workspace, check)
            receipt = (f"agent-host-{digest([check, detail])[:16]}"
                       if state in ("passed", "failed") else "")
            results.append(CheckResult(check["id"], state, receipt, detail))
        states = {r.state for r in results}
        status = ("failed" if "failed" in states else
                  "needs_review" if states - {"passed"} else "passed")
        return VerificationReport(status, tuple(results), revision="workspace")

    # -- events ------------------------------------------------------------------
    def publish(self, event: WorkflowEvent) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO events(event_id, attempt_id, payload) VALUES (?,?,?)",
                       (event.event_id, event.attempt_id, json.dumps(event.to_dict())))

    def events_after(self, attempt_id: str, cursor: int = 0, limit: int = 50) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT seq, payload FROM events WHERE attempt_id=? AND seq>? "
                              "ORDER BY seq LIMIT ?", (attempt_id, cursor, limit)).fetchall()
        return [{"cursor": r["seq"], **json.loads(r["payload"])} for r in rows]


def _job_id(operation_id: str) -> str:
    return f"agent-job-{digest(operation_id)[:12]}"
