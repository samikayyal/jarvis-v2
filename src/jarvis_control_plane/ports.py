"""Small ports and errors for the ticket01/ticket02 control-plane seam."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .trace_types import TraceReservation
    from .traces import DiagnosticTrace

from .models import (
    AuditEvidence,
    ConversationMessage,
    IngressClaim,
    OrchestrationRequest,
    OrchestrationResult,
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
    """Append-only redacted audit boundary."""

    def append(self, evidence: AuditEvidence) -> None: ...


class DurableStateStore(Protocol):
    """Authoritative local state and replay-claim port."""

    def claim_ingress(
        self,
        *,
        session_id: str,
        message_id: str,
        event_id: str,
        claimed_at: datetime,
        conversation_message: ConversationMessage | None = None,
        disposition: str = "pending_audit",
    ) -> bool: ...

    def update_ingress_disposition(
        self,
        *,
        session_id: str,
        message_id: str,
        disposition: str,
    ) -> None: ...

    def list_conversation_messages(self) -> tuple[ConversationMessage, ...]: ...

    def save_request(self, request: RequestState) -> None: ...

    def update_request(self, request: RequestState) -> None: ...

    def get_request(self, request_id: str) -> RequestState | None: ...

    def list_requests(self) -> tuple[RequestState, ...]: ...

    def list_ingress_claims(self) -> tuple[IngressClaim, ...]: ...


class OrchestrationAdapter(Protocol):
    """Non-authoritative planner boundary."""

    def run(self, request: OrchestrationRequest) -> OrchestrationResult: ...


class OutboundConnector(Protocol):
    """Closed outbound messaging capability."""

    def send(self, reply: OutboundReply) -> None: ...


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
