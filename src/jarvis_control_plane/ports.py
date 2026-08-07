"""Small ports and errors for the ticket01-ticket04 control-plane seam."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .sessions import ModelAvailability
    from .trace_types import TraceReservation
    from .traces import DiagnosticTrace

from .models import (
    AuditEvidence,
    AuditFilter,
    ConversationMessage,
    FrozenActionProposal,
    HistorySelection,
    IngressAdmissionResult,
    IngressClaim,
    OrchestrationRequest,
    OrchestrationResult,
    OutboundDelivery,
    OutboundReply,
    RequestState,
)


class ControlPlaneError(Exception):
    """Base error for an adapter boundary."""


class InvalidEnvelopeError(ControlPlaneError):
    """The signed body could not be decoded into the typed event envelope."""


class StateStoreError(ControlPlaneError):
    """Durable state could not be read or written."""


class AuditWriteError(ControlPlaneError):
    """Required redacted audit evidence could not be appended."""


class OrchestrationAdapterError(ControlPlaneError):
    """The controlled orchestration adapter failed."""


class ActionDispatcherError(ControlPlaneError):
    """A frozen approval-gated action lifecycle could not proceed."""

    def __init__(self, message: str, *, may_have_dispatched: bool = False) -> None:
        super().__init__(message)
        self.may_have_dispatched = may_have_dispatched


class ActionCancellationStatus(str, Enum):
    """The bounded acknowledgement from an action cancellation request."""

    STOPPED = "stopped"
    NOT_STARTED = "not_started"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ActionCancellationResult:
    """Typed result of asking a dispatcher to stop one prepared action."""

    status: ActionCancellationStatus | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ActionCancellationStatus(self.status))


class OutboundConnectorError(ControlPlaneError):
    """The controlled outbound connector rejected or failed a reply."""

    def __init__(self, message: str, *, may_have_sent: bool = False) -> None:
        super().__init__(message)
        self.may_have_sent = may_have_sent


class DiagnosticTraceError(ControlPlaneError):
    """The full-payload diagnostic-trace boundary failed."""


class TraceCapacityError(DiagnosticTraceError):
    """A new operation cannot reserve enough capacity for its full trace."""

    def __init__(
        self,
        message: str,
        *,
        requested_bytes: int,
        available_bytes: int,
    ) -> None:
        super().__init__(message)
        self.requested_bytes = requested_bytes
        self.available_bytes = available_bytes


class TraceWriteError(DiagnosticTraceError):
    """A complete trace could not be persisted after an operation began."""

    def __init__(self, message: str, *, operation_started: bool = False) -> None:
        super().__init__(message)
        self.operation_started = operation_started


class AuditBoundary(Protocol):
    """Append-only redacted audit boundary and safe local read surface."""

    def append(self, evidence: AuditEvidence) -> None: ...

    def append_batch(self, evidence: Sequence[AuditEvidence]) -> None: ...

    def safe_view(
        self, query: AuditFilter | None = None
    ) -> tuple[AuditEvidence, ...]: ...

    def export_json(self, query: AuditFilter | None = None) -> str: ...


class DurableStateStore(Protocol):
    """Authoritative local state and replay-claim port."""

    def admit_ingress(
        self,
        *,
        session_id: str,
        message_id: str,
        event_id: str,
        claimed_at: datetime,
        conversation_message: ConversationMessage | None,
        audit: AuditBoundary,
        audit_evidence: AuditEvidence,
        terminal_disposition: str,
        audit_blocked_disposition: str | None = None,
    ) -> IngressAdmissionResult: ...

    def claim_ingress(
        self,
        *,
        session_id: str,
        message_id: str,
        event_id: str,
        claimed_at: datetime,
        conversation_message: ConversationMessage | None = None,
        disposition: str = "admitted",
    ) -> bool: ...

    def update_ingress_disposition(
        self,
        *,
        session_id: str,
        message_id: str,
        disposition: str,
    ) -> None: ...

    def list_conversation_messages(self) -> tuple[ConversationMessage, ...]: ...

    def append_conversation_message(self, message: ConversationMessage) -> None: ...

    def reserve_outbound_conversation_message(
        self, message: ConversationMessage
    ) -> None: ...

    def accept_reserved_outbound_conversation_message(
        self,
        *,
        transport_session_id: str,
        message_id: str,
    ) -> None: ...

    def search_conversation_messages(
        self,
        *,
        text: str | None = None,
        working_session_id: str | None = None,
        request_id: str | None = None,
        direction: str | None = None,
        history_ids: tuple[str, ...] = (),
        limit: int = 50,
    ) -> tuple[ConversationMessage, ...]: ...

    def export_conversation_messages(self, **query: object) -> str: ...

    def select_history_for_context(
        self,
        *,
        text: str,
        excluding_working_session_id: str,
        limit: int = 5,
    ) -> HistorySelection: ...

    def has_ingress_claim(self, *, session_id: str, message_id: str) -> bool: ...

    def release_ingress_claim(self, *, session_id: str, message_id: str) -> bool: ...

    def save_request(self, request: RequestState) -> None: ...

    def delete_request(self, request_id: str) -> bool: ...

    def update_request(self, request: RequestState) -> None: ...

    def get_request(self, request_id: str) -> RequestState | None: ...

    def list_requests(self) -> tuple[RequestState, ...]: ...

    def list_ingress_claims(self) -> tuple[IngressClaim, ...]: ...

    def load_knowledge_vault_synchronized_at(self) -> datetime | None: ...

    def save_knowledge_vault_synchronized_at(
        self, synchronized_at: datetime
    ) -> None: ...


class OrchestrationAdapter(Protocol):
    """Non-authoritative planner boundary."""

    def run(self, request: OrchestrationRequest) -> OrchestrationResult: ...


@runtime_checkable
class ActionDispatchHandle(Protocol):
    """Prepared execution handle whose run waits for one bounded dispatch."""

    def run(self) -> object | None: ...


@runtime_checkable
class ActionDispatcher(Protocol):
    """Closed, cancellable side-effect boundary for a frozen action.

    ``cancel`` must return ``NOT_STARTED`` only when the dispatcher can prove
    that the operation never began, ``STOPPED`` only when it can prove that
    the complete side-effect scope stopped, and ``UNKNOWN`` for every lost,
    timed-out, or otherwise unconfirmed edge. A missing prepared handle is
    not proof of ``NOT_STARTED`` because the operation may have completed
    before its durable result was recorded.
    """

    def prepare(self, action: FrozenActionProposal) -> ActionDispatchHandle: ...

    def cancel(self, *, action_id: str) -> ActionCancellationResult: ...


@runtime_checkable
class ActionFinalizer(Protocol):
    """Optional retirement handshake for dispatchers with transport state."""

    def finalize(self, *, action_id: str) -> None: ...


@runtime_checkable
class BoundActionLifecycle(Protocol):
    """Optional connector lifecycle needed to bind and revalidate an action."""

    def bind_proposal(self, action: FrozenActionProposal) -> FrozenActionProposal:
        """Add immutable connector state before the proposal is presented."""
        ...

    def validate_pending_action(self, action: FrozenActionProposal) -> None:
        """Reject a frozen action whose connector state changed after binding."""
        ...


class ModelAvailabilityProvider(Protocol):
    """Authoritative provider access/availability check for exact runtime choices."""

    def current(self) -> ModelAvailability: ...


class OutboundConnector(Protocol):
    """Closed outbound capability with a side-effect-free admission check."""

    def preflight(self, reply: OutboundReply) -> None: ...

    def send(self, reply: OutboundReply) -> OutboundDelivery: ...


class DiagnosticTraceStore(Protocol):
    """Append-only full-payload trace store used before trace-producing work."""

    def reserve(
        self,
        *,
        request_id: str,
        reservation_bytes: int | None = None,
    ) -> TraceReservation: ...

    def append(
        self,
        trace: DiagnosticTrace,
        reservation: TraceReservation,
    ) -> None: ...

    def release(self, reservation: TraceReservation) -> None: ...


class Clock(Protocol):
    """Injectable time source."""

    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    """Injectable identifier source."""

    def new_id(self, namespace: str) -> str: ...


def require_non_empty(value: str, name: str) -> str:
    """Validate configuration identifiers without normalizing them silently."""

    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty canonical string")
    return value
