"""Host-facing contracts.

Plain, JSON-serializable dataclasses. Nothing here imports LangGraph: a host
adapter depends only on these types and on :mod:`langgraph_workflow.ports`.
Every ``from_dict`` validates untrusted input (identifiers, sizes, enums) and
rejects unknown keys, so persisted or transported payloads cannot smuggle in
fields the package does not understand.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any

CONTRACT_VERSION = "1"
STATE_SCHEMA_VERSION = 1

WORKFLOWS = ("verified_change", "research")
JOB_KINDS = ("inspect", "plan", "write", "review", "investigate", "synthesize")
RECEIPT_STATUSES = (
    "settled",  # host durably recorded a completed outcome
    "failed",  # executed and failed; outcome is known
    "denied",  # host policy/permission refused it; nothing executed
    "cancelled",  # stopped by cancellation; host observed quiescence
    "budget_exhausted",  # host refused admission for resource reasons
    "busy",  # workspace or slot held by another owner; retryable later
    "uncertain",  # started, outcome not durably observed; needs reconciliation
    "admitted",  # durably admitted, never started; safe for the host to start
    "running",  # in flight; the host can reattach
)
TERMINAL_RECEIPTS = ("settled", "failed", "denied", "cancelled", "budget_exhausted")
CHECK_STATES = ("passed", "failed", "needs_review", "denied", "unsupported", "stale")
STATUSES = (
    "running",
    "waiting_for_input",
    "paused",
    "waiting_for_capability",
    "budget_exhausted",
    "uncertain",
    "waiting_for_job",  # a host-run job (e.g. by an agent) has not reported yet
    "cancel_requested",
    "cancelled",
    "failed",
    "denied",
    "needs_review",
    "verified",
    "blocked",
)
TERMINAL_STATUSES = ("cancelled", "failed", "denied", "needs_review", "verified", "blocked")

MAX_TEXT = 16_000
MAX_SMALL_TEXT = 2_000
MAX_JSON_BYTES = 256_000
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,159}$")


class ContractError(ValueError):
    """An untrusted payload violated the contract."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.match(value) or ".." in value:
        raise ContractError(f"{label} must be a bounded identifier")
    return value


def text(value: Any, label: str, limit: int = MAX_TEXT, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a string")
    if len(value) > limit:
        raise ContractError(f"{label} exceeds {limit} characters")
    if required and not value.strip():
        raise ContractError(f"{label} is required")
    return value


def small_json(value: Any, label: str) -> Any:
    """Accept only plain JSON data within the payload bound."""
    try:
        encoded = canonical(value)
    except (TypeError, ValueError) as error:
        raise ContractError(f"{label} must be plain JSON data") from error
    if len(encoded.encode()) > MAX_JSON_BYTES:
        raise ContractError(f"{label} exceeds {MAX_JSON_BYTES} bytes")
    return json.loads(encoded)


def _choice(value: Any, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise ContractError(f"{label} must be one of {', '.join(allowed)}")
    return value


def _strings(value: Any, label: str, *, limit: int = 256, each: int = MAX_SMALL_TEXT) -> tuple:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ContractError(f"{label} must be a list of at most {limit} strings")
    return tuple(text(item, label, each) for item in value)


def _known(cls, value: Any) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{cls.__name__} must be an object")
    unknown = set(value) - {f.name for f in fields(cls)}
    if unknown:
        raise ContractError(f"{cls.__name__} has unknown fields: {sorted(unknown)}")
    return value


class _Record:
    def to_dict(self) -> dict[str, Any]:
        return json.loads(canonical(asdict(self)))


@dataclass(frozen=True)
class WorkflowRequest(_Record):
    """A host-admitted request. Identities are allocated by the host.

    ``attempt_id`` is the graph thread identity: a true resume keeps it, a
    new execution gets a new one. A chat session is not a thread.
    """

    workflow: str
    run_id: str
    task_id: str
    attempt_id: str
    workspace_id: str
    goal: str
    checkout_ref: str = ""
    route_ref: str = ""
    checks: tuple = ()
    plan: dict | None = None
    investigations: tuple = ()
    reviewer: bool = False
    limits: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Any) -> WorkflowRequest:
        v = _known(cls, value)
        checks = v.get("checks", ())
        if not isinstance(checks, (list, tuple)) or len(checks) > 64:
            raise ContractError("checks must be a list of at most 64 declarations")
        checks = tuple(small_json(c, "check") for c in checks)
        for check in checks:
            if not isinstance(check, dict):
                raise ContractError("each check must be an object")
            identifier(check.get("id"), "check id")
        plan = v.get("plan")
        if plan is not None:
            plan = validate_plan(plan)
        limits = small_json(v.get("limits", {}), "limits")
        if not isinstance(limits, dict) or not all(
            isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in limits.values()
        ):
            raise ContractError("limits must map names to non-negative integers")
        reviewer = v.get("reviewer", False)
        if not isinstance(reviewer, bool):
            raise ContractError("reviewer must be a boolean")
        return cls(
            workflow=_choice(v.get("workflow"), WORKFLOWS, "workflow"),
            run_id=identifier(v.get("run_id"), "run_id"),
            task_id=identifier(v.get("task_id"), "task_id"),
            attempt_id=identifier(v.get("attempt_id"), "attempt_id"),
            workspace_id=identifier(v.get("workspace_id"), "workspace_id"),
            goal=text(v.get("goal"), "goal", required=True),
            checkout_ref=text(v.get("checkout_ref", ""), "checkout_ref", MAX_SMALL_TEXT),
            route_ref=text(v.get("route_ref", ""), "route_ref", MAX_SMALL_TEXT),
            checks=checks,
            plan=plan,
            investigations=_strings(v.get("investigations", ()), "investigations", limit=16),
            reviewer=reviewer,
            limits=limits,
        )

    @property
    def fingerprint(self) -> str:
        return digest(self.to_dict())


def validate_plan(value: Any, max_steps: int = 32) -> dict:
    plan = small_json(value, "plan")
    if not isinstance(plan, dict):
        raise ContractError("plan must be an object")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= max_steps:
        raise ContractError(f"plan must have 1..{max_steps} steps")
    for step in steps:
        if not isinstance(step, dict):
            raise ContractError("plan steps must be objects")
        text(step.get("title", ""), "plan step title", MAX_SMALL_TEXT, required=True)
    return plan


@dataclass(frozen=True)
class Admission(_Record):
    """Host admission, frozen into graph state at start.

    ``policy_ref`` identifies the host policy version the attempt was admitted
    under; resume revalidates it and a revoked policy stays revoked.
    """

    admitted: bool
    policy_ref: str = ""
    reason: str = ""
    plan_approval: bool = False
    conflict_approval: bool = False
    limits: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Any) -> Admission:
        v = _known(cls, value)
        return cls(
            admitted=bool(v.get("admitted")),
            policy_ref=text(v.get("policy_ref", ""), "policy_ref", MAX_SMALL_TEXT),
            reason=text(v.get("reason", ""), "reason", MAX_SMALL_TEXT),
            plan_approval=bool(v.get("plan_approval", False)),
            conflict_approval=bool(v.get("conflict_approval", False)),
            limits=small_json(v.get("limits", {}), "limits"),
        )


@dataclass(frozen=True)
class JobSpec(_Record):
    """One bounded job the host executes through its own job/tool path.

    ``operation_id`` is the stable logical identity: re-entering a node yields
    the same id, and the host must return the recorded outcome instead of
    executing again. ``access="read"`` jobs must not change the checkout.
    """

    operation_id: str
    run_id: str
    attempt_id: str
    kind: str
    access: str
    instruction: str
    inputs: dict = field(default_factory=dict)
    parent_id: str = ""
    route_ref: str = ""
    checkout_ref: str = ""

    def __post_init__(self) -> None:
        identifier(self.operation_id, "operation_id")
        _choice(self.kind, JOB_KINDS, "job kind")
        _choice(self.access, ("read", "write"), "job access")
        text(self.instruction, "instruction")
        small_json(self.inputs, "job inputs")

    @property
    def input_fingerprint(self) -> str:
        return digest([self.kind, self.access, self.instruction, self.inputs, self.checkout_ref])


@dataclass(frozen=True)
class JobReceipt(_Record):
    """A host-recorded job outcome; ``job_id`` is queryable after restart."""

    operation_id: str
    job_id: str
    status: str
    input_fingerprint: str
    output: dict = field(default_factory=dict)
    changed_files: tuple = ()
    artifacts: tuple = ()
    evidence: tuple = ()
    usage: dict = field(default_factory=dict)
    detail: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> JobReceipt:
        v = _known(cls, value)
        output = small_json(v.get("output", {}), "output")
        if not isinstance(output, dict):
            raise ContractError("receipt output must be an object")
        return cls(
            operation_id=identifier(v.get("operation_id"), "operation_id"),
            job_id=text(v.get("job_id", ""), "job_id", MAX_SMALL_TEXT),
            status=_choice(v.get("status"), RECEIPT_STATUSES, "receipt status"),
            input_fingerprint=text(v.get("input_fingerprint", ""), "fingerprint", 128),
            output=output,
            changed_files=_strings(v.get("changed_files", ()), "changed_files", limit=4096),
            artifacts=_strings(v.get("artifacts", ()), "artifacts"),
            evidence=_strings(v.get("evidence", ()), "evidence"),
            usage=small_json(v.get("usage", {}), "usage"),
            detail=text(v.get("detail", ""), "detail", MAX_SMALL_TEXT),
        )


@dataclass(frozen=True)
class CheckResult(_Record):
    check_id: str
    state: str
    receipt_id: str = ""
    detail: str = ""


@dataclass(frozen=True)
class VerificationReport(_Record):
    """Host verification against the current execution checkout and revision.

    The package never infers success from job prose: a declared check counts
    only when the host reports ``passed`` with a receipt id.
    """

    status: str
    results: tuple = ()
    revision: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> VerificationReport:
        v = _known(cls, value)
        results = []
        for item in v.get("results", ()):
            r = _known(CheckResult, item)
            results.append(
                CheckResult(
                    check_id=identifier(r.get("check_id"), "check_id"),
                    state=_choice(r.get("state"), CHECK_STATES, "check state"),
                    receipt_id=text(r.get("receipt_id", ""), "receipt_id", MAX_SMALL_TEXT),
                    detail=text(r.get("detail", ""), "detail", MAX_SMALL_TEXT),
                )
            )
        return cls(
            status=_choice(v.get("status"), CHECK_STATES, "verification status"),
            results=tuple(results),
            revision=text(v.get("revision", ""), "revision", MAX_SMALL_TEXT),
        )


@dataclass(frozen=True)
class PendingDecision(_Record):
    """A genuine human decision the graph is waiting on."""

    decision_id: str
    run_id: str
    attempt_id: str
    kind: str
    revision: int
    digest: str
    summary: str
    options: tuple = ()


@dataclass(frozen=True)
class DecisionResponse(_Record):
    """Host-authenticated answer to a :class:`PendingDecision`.

    ``revision`` and ``digest`` must match the pending decision exactly; an
    edited plan becomes a new revision that needs its own approval.
    """

    decision_id: str
    run_id: str
    attempt_id: str
    revision: int
    digest: str
    choice: str
    edited_plan: dict | None = None
    actor: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> DecisionResponse:
        v = _known(cls, value)
        revision = v.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ContractError("revision must be a positive integer")
        edited = v.get("edited_plan")
        return cls(
            decision_id=identifier(v.get("decision_id"), "decision_id"),
            run_id=identifier(v.get("run_id"), "run_id"),
            attempt_id=identifier(v.get("attempt_id"), "attempt_id"),
            revision=revision,
            digest=text(v.get("digest"), "digest", 128, required=True),
            choice=text(v.get("choice"), "choice", 200, required=True),
            edited_plan=None if edited is None else validate_plan(edited),
            actor=text(v.get("actor", ""), "actor", MAX_SMALL_TEXT),
        )


@dataclass(frozen=True)
class AttemptStatus(_Record):
    """Read-only projection of one attempt for the host."""

    attempt_id: str
    run_id: str
    workflow: str
    status: str
    phase: str = ""
    blocker: str = ""
    detail: str = ""
    pending_decision: dict | None = None
    jobs: dict = field(default_factory=dict)
    verification: dict | None = None
    repair_rounds: int = 0
    result: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    goal: str = ""
    updated_at: float = 0.0
