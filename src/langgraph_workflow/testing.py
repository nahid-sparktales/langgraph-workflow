"""Deterministic, durable fixture host for offline tests and the demo.

Only the external host and model behavior is faked. The graph, SQLite
checkpointer, interrupts and restarts are real. Job outputs are scripted
fixture data, not model output, and are labelled as such in receipts.

State lives in ``<root>/host.sqlite3`` so a restarted process sees the same
operations, effects and events. Crash points are injected with the
``LGW_FIXTURE_CRASH`` environment variable (``"<point>:<job kind>"``) and end
the process with ``os._exit`` — no cleanup, like a real kill.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts import (
    Admission,
    CheckResult,
    DecisionResponse,
    JobReceipt,
    JobSpec,
    VerificationReport,
    WorkflowRequest,
    digest,
)
from .events import WorkflowEvent
from .filechecks import check_file, safe_path
from .ports import CAPABILITIES

CRASH_POINTS = ("before_admission", "after_admission", "during_action", "after_receipt")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops (
    operation_id TEXT PRIMARY KEY, attempt_id TEXT, kind TEXT, access TEXT,
    fingerprint TEXT, status TEXT, pid INTEGER, started INTEGER DEFAULT 0,
    receipt TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS effects (id INTEGER PRIMARY KEY, operation_id TEXT, at REAL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, attempt_id TEXT,
    payload TEXT);
CREATE TABLE IF NOT EXISTS admissions (attempt_id TEXT PRIMARY KEY, policy_ref TEXT,
    revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cancels (attempt_id TEXT PRIMARY KEY);
"""

DEFAULT_SCENARIO: dict[str, Any] = {
    "capabilities": sorted(CAPABILITIES),
    "admit": True,
    "admit_reason": "",
    "plan_approval": False,
    "conflict_approval": False,
    "limits": {},
    "actors": ["user"],
    "plan": {"steps": [{"title": "Write the result file"}]},
    "writes": {"implement": {"result.txt": "draft\n"}, "repair": [{"result.txt": "done\n"}]},
    "review": ["approve"],
    "findings": {},
    "synthesis": {"summary": "fixture synthesis", "claims": {}, "sources": []},
    "fail": {},  # job kind -> failed | denied | budget_exhausted | busy
    "job_delay": 0.0,
    "forge_verification": False,
}


class FixtureHost:
    def __init__(self, root: str | os.PathLike, scenario: dict | None = None) -> None:
        self.root = Path(root)
        self.workspace = self.root / "workspace"
        self.scenario = {**DEFAULT_SCENARIO, **(scenario or {})}
        self.db_path = self.root / "host.sqlite3"
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self.active_reads = 0
        self.max_active_reads = 0
        self.workspace.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)

    @classmethod
    def from_file(cls, root: str | os.PathLike) -> FixtureHost:
        scenario = Path(root, "scenario.json")
        return cls(root, json.loads(scenario.read_text()) if scenario.exists() else None)

    def save_scenario(self) -> None:
        Path(self.root, "scenario.json").write_text(json.dumps(self.scenario))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def _crash(self, point: str, spec: JobSpec) -> None:
        if os.environ.get("LGW_FIXTURE_CRASH") == f"{point}:{spec.kind}":
            os._exit(137)

    # -- admission ---------------------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        return frozenset(self.scenario["capabilities"])

    def admit(self, request: WorkflowRequest) -> Admission:
        if not self.scenario["admit"]:
            return Admission(False, reason=self.scenario["admit_reason"] or "not_admitted")
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO admissions(attempt_id, policy_ref) VALUES (?,?)",
                       (request.attempt_id, "fixture-policy/1"))
        return self._admission("fixture-policy/1")

    def _admission(self, policy_ref: str) -> Admission:
        return Admission(True, policy_ref=policy_ref,
                         plan_approval=self.scenario["plan_approval"],
                         conflict_approval=self.scenario["conflict_approval"],
                         limits=self.scenario["limits"])

    def revalidate(self, request: WorkflowRequest, policy_ref: str) -> Admission:
        with self._db() as db:
            row = db.execute("SELECT * FROM admissions WHERE attempt_id=?",
                             (request.attempt_id,)).fetchone()
        if row is None or row["revoked"] or row["policy_ref"] != policy_ref:
            return Admission(False, reason="policy_revoked")
        if request.checkout_ref and request.checkout_ref != self.scenario.get(
                "checkout_ref", request.checkout_ref):
            return Admission(False, reason="checkout_changed")
        return self._admission(policy_ref)

    def revoke(self, attempt_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE admissions SET revoked=1 WHERE attempt_id=?", (attempt_id,))

    def authorize_decision(self, response: DecisionResponse) -> bool:
        return response.actor in self.scenario["actors"]

    # -- jobs --------------------------------------------------------------------
    def lookup(self, operation_id: str) -> JobReceipt | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM ops WHERE operation_id=?",
                             (operation_id,)).fetchone()
        return None if row is None else self._receipt(row)

    def _receipt(self, row: sqlite3.Row) -> JobReceipt:
        if row["receipt"]:
            return JobReceipt.from_dict(json.loads(row["receipt"]))
        if not row["started"]:
            status = "admitted"
        elif row["operation_id"] in self._running:
            status = "running"
        else:
            status = "uncertain"  # started, outcome never durably observed
        return JobReceipt(row["operation_id"], _job_id(row["operation_id"]), status,
                          row["fingerprint"], detail="" if status != "uncertain"
                          else "started_without_observed_outcome")

    def execute(self, spec: JobSpec) -> JobReceipt:
        self._crash("before_admission", spec)
        refusal = self.scenario["fail"].get(spec.kind)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM ops WHERE operation_id=?",
                             (spec.operation_id,)).fetchone()
            if row is not None:
                db.execute("COMMIT")
                if row["fingerprint"] != spec.input_fingerprint:
                    return JobReceipt(spec.operation_id, _job_id(spec.operation_id), "uncertain",
                                      spec.input_fingerprint, detail="operation_conflict")
                receipt = self._receipt(row)
                if receipt.status != "admitted":
                    return receipt  # never start an operation twice
            else:
                status = None
                if db.execute("SELECT 1 FROM cancels WHERE attempt_id=?",
                              (spec.attempt_id,)).fetchone():
                    status = "cancelled"
                elif refusal in ("denied", "budget_exhausted"):
                    status = refusal
                elif spec.access == "write" and db.execute(
                        "SELECT 1 FROM ops WHERE access='write' AND receipt IS NULL "
                        "AND started=1 AND attempt_id!=?", (spec.attempt_id,)).fetchone():
                    status = "busy"  # single writer per fixture workspace
                if status is not None:
                    receipt = JobReceipt(spec.operation_id, "", status, spec.input_fingerprint,
                                         detail=f"host refused before execution: {status}")
                    if status in ("cancelled", "denied"):  # sticky; others re-evaluate
                        db.execute("INSERT INTO ops VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (spec.operation_id, spec.attempt_id, spec.kind, spec.access,
                                    spec.input_fingerprint, status, os.getpid(), 0,
                                    json.dumps(receipt.to_dict()), time.time()))
                    db.execute("COMMIT")
                    return receipt
                db.execute("INSERT INTO ops VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (spec.operation_id, spec.attempt_id, spec.kind, spec.access,
                            spec.input_fingerprint, "admitted", os.getpid(), 0, None,
                            time.time()))
                db.execute("COMMIT")
        self._crash("after_admission", spec)
        with self._db() as db:
            db.execute("UPDATE ops SET started=1, pid=? WHERE operation_id=?",
                       (os.getpid(), spec.operation_id))
            db.execute("INSERT INTO effects(operation_id, at) VALUES (?,?)",
                       (spec.operation_id, time.time()))
        with self._lock:
            self._running.add(spec.operation_id)
            if spec.access == "read":
                self.active_reads += 1
                self.max_active_reads = max(self.max_active_reads, self.active_reads)
        try:
            status, output, changed = self._perform(spec, refusal)
        finally:
            with self._lock:
                self._running.discard(spec.operation_id)
                if spec.access == "read":
                    self.active_reads -= 1
        receipt = JobReceipt(
            spec.operation_id, _job_id(spec.operation_id), status, spec.input_fingerprint,
            output=output, changed_files=tuple(changed), evidence=(f"fixture:{spec.kind}",),
            usage={"calls": 1, "source": "fixture",
                   **self.scenario.get("usage", {"coverage": "known"})},
            detail="fixture output" if status == "settled" else status,
        )
        with self._db() as db:
            db.execute("UPDATE ops SET status=?, receipt=?, updated_at=? WHERE operation_id=?",
                       (status, json.dumps(receipt.to_dict()), time.time(), spec.operation_id))
        self._crash("after_receipt", spec)
        return receipt

    def _cancelled(self, attempt_id: str) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM cancels WHERE attempt_id=?",
                              (attempt_id,)).fetchone() is not None

    def _perform(self, spec: JobSpec, refusal: str | None) -> tuple[str, dict, list]:
        s = self.scenario
        deadline = time.monotonic() + float(s["job_delay"])
        while time.monotonic() < deadline:
            if self._cancelled(spec.attempt_id):
                return "cancelled", {}, []
            time.sleep(0.01)
        if refusal == "failed":
            return "failed", {}, []
        if spec.kind == "inspect":
            return "settled", {"summary": "fixture workspace context"}, []
        if spec.kind == "plan":
            return "settled", {"plan": s["plan"]}, []
        if spec.kind == "review":
            index = int(spec.operation_id.rsplit("-", 1)[1]) - 1
            verdicts = s["review"]
            verdict = verdicts[min(index, len(verdicts) - 1)]
            return "settled", {"verdict": verdict,
                               "findings": [] if verdict == "approve" else ["fixture finding"]}, []
        if spec.kind == "investigate":
            return "settled", s["findings"].get(str(spec.inputs["index"]), {}), []
        if spec.kind == "synthesize":
            return "settled", s["synthesis"], []
        # write: implement or repair-N
        if spec.operation_id.split("/")[-1].startswith("repair-"):
            index = int(spec.operation_id.rsplit("-", 1)[1]) - 1
            repairs = s["writes"].get("repair", [])
            files = repairs[index] if index < len(repairs) else {}
        else:
            files = s["writes"].get("implement", {})
        changed = []
        for relative, content in sorted(files.items()):
            path = safe_path(self.workspace, relative)
            before = path.read_bytes() if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode()
            half = len(data) // 2
            path.write_bytes(data[:half])
            self._crash("during_action", spec)
            if self._cancelled(spec.attempt_id):
                return "cancelled", {}, changed + [relative]
            path.write_bytes(data)
            if before != data:
                changed.append(relative)
        return "settled", {"summary": f"wrote {len(changed)} file(s)"}, changed

    def cancel(self, attempt_id: str) -> bool:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO cancels VALUES (?)", (attempt_id,))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self._db() as db:
                rows = db.execute("SELECT operation_id, pid FROM ops WHERE attempt_id=? "
                                  "AND started=1 AND receipt IS NULL", (attempt_id,)).fetchall()
            live = [r for r in rows if r["operation_id"] in self._running
                    or (r["pid"] != os.getpid() and _pid_alive(r["pid"]))]
            if not live:
                return True
            time.sleep(0.01)
        return False

    def reconcile(self, operation_id: str, status: str, note: str) -> JobReceipt:
        """Human reconciliation of an uncertain operation. Never re-executes it."""
        assert status in ("settled", "failed")
        with self._db() as db:
            row = db.execute("SELECT * FROM ops WHERE operation_id=?",
                             (operation_id,)).fetchone()
            receipt = JobReceipt(operation_id, _job_id(operation_id), status, row["fingerprint"],
                                 output={"summary": note}, detail=f"reconciled: {note}")
            db.execute("UPDATE ops SET status=?, receipt=? WHERE operation_id=?",
                       (status, json.dumps(receipt.to_dict()), operation_id))
        return receipt

    def effect_count(self, operation_id: str | None = None) -> int:
        with self._db() as db:
            if operation_id is None:
                return db.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
            return db.execute("SELECT COUNT(*) FROM effects WHERE operation_id=?",
                              (operation_id,)).fetchone()[0]

    def operations(self) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM ops ORDER BY rowid")]

    # -- verification ------------------------------------------------------------
    def verify(self, request: WorkflowRequest, checks: tuple, *, final: bool) -> VerificationReport:
        results = []
        for check in checks:
            state, detail = self._check(check)
            receipt = ""
            if state in ("passed", "failed") and not self.scenario["forge_verification"]:
                receipt = f"fixture-receipt-{digest([check, detail])[:16]}"
            results.append(CheckResult(check["id"], state, receipt, detail))
        if self.scenario["forge_verification"]:
            results = [CheckResult(r.check_id, "passed", "", "forged") for r in results]
        states = {r.state for r in results}
        status = ("failed" if "failed" in states else
                  "needs_review" if states - {"passed"} else "passed")
        return VerificationReport(status, tuple(results), revision="fixture-revision-1")

    def _check(self, check: dict) -> tuple[str, str]:
        return check_file(self.workspace, check)

    # -- events ------------------------------------------------------------------
    def publish(self, event: WorkflowEvent) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO events(event_id, attempt_id, payload) VALUES (?,?,?)",
                       (event.event_id, event.attempt_id, json.dumps(event.to_dict())))

    def events_after(self, cursor: int = 0, limit: int = 100,
                     attempt_id: str | None = None) -> list[dict]:
        """Durable replay from a host cursor, in bounded pages."""
        sql = "SELECT seq, payload FROM events WHERE seq>?"
        args: list[Any] = [cursor]
        if attempt_id:
            sql += " AND attempt_id=?"
            args.append(attempt_id)
        with self._db() as db:
            rows = db.execute(sql + " ORDER BY seq LIMIT ?", (*args, min(limit, 1000))).fetchall()
        return [{"cursor": r["seq"], **json.loads(r["payload"])} for r in rows]


def _job_id(operation_id: str) -> str:
    return f"fixture-job-{digest(operation_id)[:12]}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_host_contract(host: Any, request: WorkflowRequest, *,
                      count_events: Any) -> dict[str, str]:
    """Behavioral contract every :class:`WorkflowHost` must satisfy.

    Runs against a fresh host whose write job changes at least one file.
    ``count_events(event_id)`` returns how many times the host stored that
    event. Returns ``{check: "pass" | "fail: reason"}``; hosts share it so the
    fixture host and a real adapter are held to the same rules.
    """
    from .events import make_event
    from .ports import CAPABILITIES as KNOWN

    results: dict[str, str] = {}

    def check(name: str, ok: bool, why: str = "") -> None:
        results[name] = "pass" if ok else f"fail: {why}"

    base = request.attempt_id

    def job(key: str, kind: str, access: str, instruction: str = "contract") -> JobSpec:
        return JobSpec(f"{base}/{key}", request.run_id, base, kind, access, instruction,
                       checkout_ref=request.checkout_ref)

    caps = host.capabilities()
    check("capabilities_are_known", caps <= KNOWN, str(sorted(caps - KNOWN)))
    first, second = host.admit(request), host.admit(request)
    check("admit_is_idempotent", first.admitted and first.policy_ref == second.policy_ref)
    check("revalidate_rejects_other_policy",
          not host.revalidate(request, "some-other-policy").admitted)
    check("revalidate_accepts_saved_policy", host.revalidate(request, first.policy_ref).admitted)
    check("lookup_unknown_is_none", host.lookup(f"{base}/never") is None)
    read = job("contract-read", "inspect", "read")
    receipt = host.execute(read)
    check("read_job_settles_without_changes",
          receipt.status == "settled" and not receipt.changed_files and bool(receipt.job_id),
          f"{receipt.status} {receipt.changed_files}")
    again = host.execute(read)
    check("execute_is_idempotent", again.job_id == receipt.job_id and again.status == "settled",
          f"{again.job_id} != {receipt.job_id}")
    check("lookup_returns_recorded_outcome", (host.lookup(read.operation_id) or again).job_id
          == receipt.job_id)
    conflicting = job("contract-read", "inspect", "read", "different input")
    conflict = host.execute(conflicting)
    check("fingerprint_conflict_is_not_executed",
          conflict.input_fingerprint != conflicting.input_fingerprint
          or conflict.status == "uncertain", f"{conflict.status}")
    write = host.execute(job("contract-write", "write", "write"))
    check("write_job_reports_changes", write.status == "settled" and bool(write.changed_files),
          f"{write.status} {write.changed_files}")
    report = host.verify(request, (
        {"id": "contract-absent", "kind": "file_contains", "path": "result.txt",
         "value": "never-present-value", "requirement": "absent value"},
        {"id": "contract-human", "kind": "human_review", "requirement": "owner review"},
    ), final=False)
    by_id = {r.check_id: r for r in report.results}
    absent, human = by_id.get("contract-absent"), by_id.get("contract-human")
    check("failed_check_is_failed_with_receipt",
          absent is not None and absent.state == "failed" and bool(absent.receipt_id),
          str(absent))
    check("human_review_is_never_passed", human is not None and human.state == "needs_review",
          str(human))
    check("report_is_not_passed", report.status != "passed", report.status)
    event = make_event("workflow.started", request.to_dict(), key="contract")
    host.publish(event)
    host.publish(event)
    stored = count_events(event.event_id)
    check("publish_deduplicates_event_id", stored == 1, f"stored {stored}")
    intruder = DecisionResponse("d", request.run_id, base, 1, "0" * 64, "approve",
                                actor="intruder-not-authenticated")
    check("unauthenticated_decision_is_refused", not host.authorize_decision(intruder))
    check("idle_cancel_is_quiescent", host.cancel(base) is True)
    after = host.execute(job("contract-after-cancel", "inspect", "read"))
    check("no_new_job_starts_after_cancel", after.status == "cancelled", after.status)
    return results


class _DieOnCommit:
    """Connection proxy that kills the process instead of committing."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def commit(self) -> None:
        os._exit(137)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _arm_checkpoint_crash(host: FixtureHost) -> None:
    """LGW_CRASH_ON_CHECKPOINT=after_write: die inside the first checkpoint
    transaction that follows a settled writer job (before it commits)."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    armed = {"write_settled": False}
    execute, put = host.execute, SqliteSaver.put

    def tracking_execute(spec: JobSpec) -> JobReceipt:
        receipt = execute(spec)
        if spec.access == "write" and receipt.status == "settled":
            armed["write_settled"] = True
        return receipt

    def dying_put(self, *args: Any, **kwargs: Any):
        if armed["write_settled"]:
            self.conn = _DieOnCommit(self.conn)
        return put(self, *args, **kwargs)

    host.execute = tracking_execute  # type: ignore[method-assign]
    SqliteSaver.put = dying_put  # type: ignore[method-assign]


def main(argv: list[str] | None = None) -> int:
    """``python -m langgraph_workflow.testing ROOT ACTION [ARG]`` — drive one
    executor call in a fresh process and print the attempt status as JSON.

    ACTION is start (ARG: request JSON file), decide (ARG: response JSON
    file), resume|pause|cancel|status (ARG: attempt id), or events.
    """
    import sys

    from .executor import DecisionRejected, WorkflowExecutor

    root, action, *rest = argv if argv is not None else sys.argv[1:]
    host = FixtureHost.from_file(root)
    if os.environ.get("LGW_CRASH_ON_CHECKPOINT") == "after_write":
        _arm_checkpoint_crash(host)
    executor = WorkflowExecutor(host, Path(root, "checkpoints.sqlite3"))
    try:
        if action == "events":
            print(json.dumps(host.events_after(int(rest[0]) if rest else 0, 1000)))
            return 0
        if action in ("start", "decide"):
            payload = json.loads(Path(rest[0]).read_text())
            status = executor.start(payload) if action == "start" else executor.decide(payload)
        else:
            status = getattr(executor, action)(rest[0])
    except DecisionRejected as error:
        print(json.dumps({"rejected": error.code}))
        return 3
    finally:
        executor.close()
    print(json.dumps(status.to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
