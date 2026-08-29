"""Errors and small values shared by application ports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ControlPlaneError(Exception):
    """Base error for an adapter boundary."""


class InvalidEnvelopeError(ControlPlaneError):
    """The signed body could not be decoded into the typed event envelope."""


class StateStoreError(ControlPlaneError):
    """Durable state could not be read or written."""

    def __init__(self, message: str, *, may_have_dispatched: bool = False) -> None:
        super().__init__(message)
        self.may_have_dispatched = may_have_dispatched


class DeletedConversationArchiveError(ControlPlaneError):
    """The manual-administration archival boundary could not accept content."""


class MemorySearchLimitExceeded(StateStoreError):
    """A durable-memory search reached its deterministic scan ceiling."""

    def __init__(self, message: str, *, scanned_rows: int) -> None:
        super().__init__(message)
        self.scanned_rows = scanned_rows


class AuditWriteError(ControlPlaneError):
    """Required redacted audit evidence could not be appended."""


class OrchestrationAdapterError(ControlPlaneError):
    """The controlled orchestration adapter failed."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None and (not code or code.strip() != code):
            raise ValueError("orchestration diagnostic code must be canonical")
        self.code = code


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
