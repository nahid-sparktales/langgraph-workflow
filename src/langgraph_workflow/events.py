"""Versioned, redacted event contract.

Graph internals (state, checkpoints, stream chunks, interrupt objects) never
leave the package. Nodes emit small typed events; ``event_id`` is derived from
stable identities, so a node that LangGraph re-enters produces the same id and
the host's durable store drops the duplicate. The host allocates the
authoritative replay cursor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import digest

EVENT_SCHEMA = "langgraph-workflow.event/1"
EVENT_TYPES = frozenset(
    {
        "workflow.started",
        "workflow.phase",
        "workflow.routing",
        "job.submitted",
        "job.outcome",
        "decision.pending",
        "decision.resolved",
        "verification.result",
        "workflow.paused",
        "workflow.outcome",
    }
)

_SECRET_KEY = re.compile(
    r"(api[_-]?key|authorization|cookie|credential|password|passphrase|secret|signature|"
    r"private[_-]?key|access[_-]?token|refresh[_-]?token|bearer|^token$)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
MAX_STRING = 2_000
MAX_ITEMS = 64
MAX_DEPTH = 6


def redact(value: Any, depth: int = 0) -> Any:
    """Bounded, secret-free copy of plain data for user-facing events."""
    if depth > MAX_DEPTH:
        return "[truncated]"
    if isinstance(value, dict):
        out = {}
        for key in sorted(value, key=str)[:MAX_ITEMS]:
            k = str(key)[:128]
            out[k] = "[redacted]" if _SECRET_KEY.search(k) else redact(value[key], depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, depth + 1) for item in list(value)[:MAX_ITEMS]]
    if isinstance(value, str):
        cleaned = _SECRET_VALUE.sub("[redacted]", value)
        return cleaned if len(cleaned) <= MAX_STRING else cleaned[:MAX_STRING] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(type(value).__name__)


@dataclass(frozen=True)
class WorkflowEvent:
    event_id: str
    type: str
    run_id: str
    attempt_id: str
    workflow: str
    operation_id: str = ""
    parent_id: str = ""
    payload: dict = field(default_factory=dict)
    diagnostic: bool = False
    schema: str = EVENT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "type": self.type,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "workflow": self.workflow,
            "operation_id": self.operation_id,
            "parent_id": self.parent_id,
            "payload": self.payload,
            "diagnostic": self.diagnostic,
        }


def make_event(
    kind: str,
    request: dict,
    *,
    key: str,
    operation_id: str = "",
    parent_id: str = "",
    payload: dict | None = None,
    diagnostic: bool = False,
) -> WorkflowEvent:
    """Build an event whose id is stable for the same logical occurrence."""
    if kind not in EVENT_TYPES:
        raise ValueError(f"unknown event type {kind}")
    return WorkflowEvent(
        event_id=digest([request["attempt_id"], kind, operation_id, key])[:32],
        type=kind,
        run_id=request["run_id"],
        attempt_id=request["attempt_id"],
        workflow=request["workflow"],
        operation_id=operation_id,
        parent_id=parent_id,
        payload=redact(payload or {}),
        diagnostic=diagnostic,
    )
