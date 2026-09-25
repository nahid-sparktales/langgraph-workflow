"""Host-agnostic LangGraph workflow executor.

Importing this package has no side effects: no I/O, threads, logging
configuration, environment changes or database initialization.
"""

from .checkpoints import AttemptBusy, EncryptionRequired
from .contracts import (
    CONTRACT_VERSION,
    STATE_SCHEMA_VERSION,
    Admission,
    AttemptStatus,
    CheckResult,
    ContractError,
    DecisionResponse,
    JobReceipt,
    JobSpec,
    PendingDecision,
    VerificationReport,
    WorkflowRequest,
)
from .events import EVENT_SCHEMA, WorkflowEvent
from .executor import DecisionRejected, IncompatibleAttempt, WorkflowExecutor
from .ports import CAPABILITIES, WorkflowHost

__version__ = "0.4.1"

__all__ = [
    "CAPABILITIES", "CONTRACT_VERSION", "EVENT_SCHEMA", "STATE_SCHEMA_VERSION", "Admission",
    "AttemptBusy", "AttemptStatus", "CheckResult", "ContractError", "DecisionRejected",
    "DecisionResponse", "EncryptionRequired", "IncompatibleAttempt", "JobReceipt", "JobSpec",
    "PendingDecision", "VerificationReport", "WorkflowEvent", "WorkflowExecutor",
    "WorkflowHost", "WorkflowRequest",
]
