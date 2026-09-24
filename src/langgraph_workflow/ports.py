"""The single capability surface a host runtime injects.

The package never obtains credentials, invokes models, runs tools, or touches
files itself. Everything effectful goes through a :class:`WorkflowHost`, which
remains authoritative for identities, admission, authorization, provider
choice, tools, workspaces, spend, verification status and memory.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .contracts import (
    Admission,
    DecisionResponse,
    JobReceipt,
    JobSpec,
    VerificationReport,
    WorkflowRequest,
)
from .events import WorkflowEvent

# Capability names a workflow can require. Hosts report the subset they
# actually support; missing ones stop the workflow before any job runs.
CAPABILITIES = frozenset(
    {
        "jobs.read",  # bounded read-only jobs (inspect/plan/review/investigate/synthesize)
        "jobs.write",  # bounded writer jobs through the host's single-writer path
        "verify",  # host acceptance-check machinery with receipts
        "decisions",  # authenticated human decisions
        "events",  # durable event delivery with host-allocated cursors
        "cancel",  # propagates cancellation to active jobs and confirms quiescence
    }
)


@runtime_checkable
class WorkflowHost(Protocol):
    # -- admission and policy ------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        """Capabilities this host supports right now."""

    def admit(self, request: WorkflowRequest) -> Admission:
        """Admit (or refuse) an attempt. Idempotent per ``attempt_id``."""

    def revalidate(self, request: WorkflowRequest, policy_ref: str) -> Admission:
        """Re-check a saved attempt against current host policy before resume.

        Must return ``admitted=False`` when permission was revoked, the saved
        route/account is unavailable, or the execution checkout changed. The
        host must not silently substitute another provider or account.
        """

    # -- bounded jobs ----------------------------------------------------------
    def execute(self, spec: JobSpec) -> JobReceipt:
        """Durably admit, authorize and run one bounded job; block until settled.

        Idempotent by ``operation_id``: a known operation returns its recorded
        outcome, reattaches to in-flight work, or reports ``uncertain``. A
        started operation whose outcome was never observed must never be
        started again. A different ``input_fingerprint`` for a known
        ``operation_id`` must be refused.
        """

    def lookup(self, operation_id: str) -> JobReceipt | None:
        """Recorded outcome for an operation, or ``None`` if never admitted."""

    def cancel(self, attempt_id: str) -> bool:
        """Cancel queued/active jobs of an attempt. True once quiescent."""

    # -- verification ----------------------------------------------------------
    def verify(
        self, request: WorkflowRequest, checks: tuple, *, final: bool
    ) -> VerificationReport:
        """Run declared checks through the host's verification machinery.

        With ``final=True`` the host may answer from existing evidence only
        when it can prove that evidence current (same revision, requirements
        and file fingerprints); otherwise it must run the checks again.
        """

    # -- decisions and events --------------------------------------------------
    def authorize_decision(self, response: DecisionResponse) -> bool:
        """True when the response came from an authenticated, authorized actor."""

    def publish(self, event: WorkflowEvent) -> None:
        """Durably deliver one event. Must ignore a repeated ``event_id``."""
