# ruff: noqa: F401, I001, RUF100 -- shared namespace supplies workflow mixins.
"""Receiver and deterministic capability-broker path for ticket01/ticket03."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime
from threading import RLock

from ...control_grammar import (
    ApprovalChoice,
    ControlCommand,
    ControlTransition,
    ControlTransitionKind,
    MessageKind,
    admit_orchestration_request,
    handle_message,
    parse_approval_choice,
    parse_control,
)
from ...memory import (
    DurableMemoryActionDispatcher,
    MemoryCommand,
    MemoryOperation,
    parse_memory_command,
)
from ...models import (
    AuditEvidence,
    ConversationDeletionPreview,
    ConversationDeletionScope,
    ConversationMessage,
    DurableMemory,
    FrozenActionProposal,
    InboundMessage,
    MemorySelection,
    OrchestrationProposalIntent,
    OrchestrationRequest,
    OrchestrationResult,
    OutboundAttemptRecoveryProjection,
    OutboundAttemptStatus,
    OutboundDelivery,
    OutboundReply,
    ReceiveResult,
    RecoveryDegradedMarker,
    RequestState,
    SignedInboundEvent,
)
from ...ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcher,
    ActionDispatcherError,
    ActionDispatchHandle,
    ActionFinalizer,
    AuditBoundary,
    AuditWriteError,
    BoundActionLifecycle,
    Clock,
    ConnectedServiceReadinessProvider,
    DiagnosticTraceError,
    DurableStateStore,
    IdGenerator,
    KnowledgeVaultWriteProposalPreparer,
    MemorySearchLimitExceeded,
    MessagingGatewayReadinessProvider,
    ModelAvailabilityProvider,
    OrchestrationAdapter,
    OrchestrationAdapterError,
    OutboundConnector,
    OutboundConnectorError,
    StateStoreError,
    TraceCapacityError,
    TraceWriteError,
    WorkerReadinessProvider,
    require_non_empty,
)
from ...sessions import (
    CancellationToken,
    CommandPermissionState,
    DispatchStatus,
    InMemoryWorkingSessionStore,
    InvariantViolation,
    ModelAvailability,
    PendingActionState,
    PermissionLifetime,
    ProposalPresentationStatus,
    RequestResult,
    ServiceReadiness,
    SessionConfig,
    SessionStoreError,
    SessionTransition,
    TransitionKind,
    WorkingSession,
    WorkingSessionStore,
    accept_request,
    apply_request_result,
    approve_pending_action,
    cancel_active_request,
    cancellation_token_is_current,
    complete_action_dispatch,
    expire_inactive_session,
    expire_pending_action,
    install_pending_action,
    interrupt_for_restart,
    mark_action_dispatch_attempted,
    mark_proposal_presented,
    reconcile_action_cancellation,
    record_proposal_fragment,
    reject_pending_action,
)
from ...terminal_policy import (
    TerminalAction,
    TerminalDisposition,
    authorize_terminal_proposal,
    terminal_action_from_proposal,
)
from ...traces import DiagnosticTraceRecorder
from ...worker_gateway import WorkerExecutionResult

_MAX_RAW_INBOUND_BODY_BYTES = 128 * 1024
_PROPOSAL_FRAGMENT_PAYLOAD_CHARS = 3_000
_MAX_OUTBOUND_MESSAGE_CHARS = 4_096
_INFORMATIONAL_TRUNCATION_MARKER = " [truncated]"


def _bounded_informational_reply(reply_text: str, *, request_id: str) -> str:
    """Keep a non-approval response within the fixed outbound message ceiling."""

    suffix = f" (request_id={request_id})"
    body = f"{reply_text}{suffix}"
    if len(body) <= _MAX_OUTBOUND_MESSAGE_CHARS:
        return body
    content_limit = (
        _MAX_OUTBOUND_MESSAGE_CHARS
        - len(_INFORMATIONAL_TRUNCATION_MARKER)
        - len(suffix)
    )
    return f"{reply_text[:content_limit]}{_INFORMATIONAL_TRUNCATION_MARKER}{suffix}"


def _render_terminal_result(
    terminal: TerminalAction, result: WorkerExecutionResult
) -> str:
    """Render exact bounded streams without confusing output with instructions."""

    return "\n".join(
        (
            f"Execution host: {terminal.host}",
            f"Execution status: {result.status.value}",
            f"stdout_truncated: {str(result.stdout_truncated).lower()}",
            f"stderr_truncated: {str(result.stderr_truncated).lower()}",
            f"stdout JSON: {json.dumps(result.stdout, ensure_ascii=False)}",
            f"stderr JSON: {json.dumps(result.stderr, ensure_ascii=False)}",
        )
    )


def _deletion_payload(preview: ConversationDeletionPreview) -> dict[str, object]:
    """Freeze only exact selectors and content evidence, never message bodies."""

    scope = preview.scope
    return {
        "content_digest": preview.content_digest,
        "conversation_id": scope.conversation_id,
        "end_at": scope.end_at.isoformat() if scope.end_at is not None else None,
        "history_ids": list(preview.history_ids),
        "scope_type": scope.scope_type,
        "start_at": scope.start_at.isoformat() if scope.start_at is not None else None,
    }


def _deletion_scope_from_payload(payload: object) -> ConversationDeletionScope:
    if not isinstance(payload, dict):
        raise TypeError("conversation deletion payload must be an object")
    scope_type = payload.get("scope_type")
    if scope_type == "message":
        history_ids = payload.get("history_ids")
        if not isinstance(history_ids, list) or not all(
            isinstance(value, str) for value in history_ids
        ):
            raise ValueError(
                "conversation deletion payload has invalid message selectors"
            )
        return ConversationDeletionScope.message(tuple(history_ids))
    if scope_type == "conversation":
        conversation_id = payload.get("conversation_id")
        if not isinstance(conversation_id, str):
            raise ValueError("conversation deletion payload has no conversation ID")
        return ConversationDeletionScope.conversation(conversation_id)
    if scope_type == "date_range":
        start_at = payload.get("start_at")
        end_at = payload.get("end_at")
        if not isinstance(start_at, str) or not isinstance(end_at, str):
            raise ValueError("conversation deletion payload has no date range")
        return ConversationDeletionScope.date_range(
            datetime.fromisoformat(start_at), datetime.fromisoformat(end_at)
        )
    raise ValueError("conversation deletion payload has an invalid scope")


def _dispatch_failure_status(error: BaseException) -> DispatchStatus:
    """Translate one dispatch failure into the durable outcome exactly once."""

    trace_error = error if isinstance(error, TraceWriteError) else error.__cause__
    if isinstance(trace_error, TraceWriteError) and trace_error.operation_started:
        return DispatchStatus.UNKNOWN
    if isinstance(error, DiagnosticTraceError):
        return DispatchStatus.NOT_STARTED
    if isinstance(error, ActionDispatcherError):
        return (
            DispatchStatus.UNKNOWN
            if error.may_have_dispatched
            else DispatchStatus.FAILED
        )
    return DispatchStatus.UNKNOWN


def _dispatch_disposition(status: DispatchStatus) -> str:
    return {
        DispatchStatus.UNKNOWN: "action_dispatch_unknown",
        DispatchStatus.NOT_STARTED: "action_dispatch_not_started",
        DispatchStatus.FAILED: "action_dispatch_failed",
    }.get(status, "action_dispatch_unknown")


def _memory_action_operation(payload: str) -> str:
    """Read only the bounded operation name for metadata-only audit evidence."""

    try:
        value = json.loads(payload).get("operation")
    except (TypeError, json.JSONDecodeError, AttributeError):
        return "unknown"
    return value if isinstance(value, str) and value else "unknown"


class _CancelledBeforeDispatch(OutboundConnectorError):
    """The request lost ownership before its outbound operation started."""


class _UnavailableActionDispatcher:
    """Fail closed until a later ticket supplies a concrete capability edge."""

    def prepare(self, action: FrozenActionProposal) -> ActionDispatchHandle:
        raise ActionDispatcherError("no action dispatcher is configured")

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)


@dataclass(frozen=True, slots=True)
class _PreparedActionDispatch:
    """The registered handle and exact frozen payload waiting outside the lock."""

    action: FrozenActionProposal
    handle: ActionDispatchHandle


@dataclass(frozen=True, slots=True)
class _ApprovedActionDispatch:
    """Approved action metadata whose external preparation may run unlocked."""

    action: PendingActionState
    terminal: TerminalAction | None
    permission_id: str | None


@dataclass(frozen=True, slots=True)
class _CancellationOutcome:
    """One bounded dispatcher acknowledgement and its durable interpretation."""

    action_id: str
    kind: str
    result: ActionCancellationResult
    durable_status: DispatchStatus


class _NoopActionLifecycle:
    """Identity lifecycle for actions whose dispatcher has no external binding."""

    def bind_proposal(self, action: FrozenActionProposal) -> FrozenActionProposal:
        return action

    def validate_pending_action(self, action: FrozenActionProposal) -> None:
        return


class _ConversationDeletionDispatch:
    """Prepared local deletion edge for one exact frozen history selection."""

    def __init__(
        self,
        *,
        state: DurableStateStore,
        action: FrozenActionProposal,
        clock: Clock,
    ) -> None:
        self._state = state
        self._action = action
        self._clock = clock
        self._lock = RLock()
        self._started = False
        self._cancelled = False

    def run(self) -> None:
        with self._lock:
            if self._cancelled:
                raise ActionDispatcherError(
                    "conversation deletion was cancelled before dispatch"
                )
            self._started = True
        try:
            payload = json.loads(self._action.payload)
            scope = _deletion_scope_from_payload(payload)
            if not isinstance(payload, dict):
                raise TypeError("conversation deletion payload must be an object")
            raw_history_ids = payload.get("history_ids")
            if not isinstance(raw_history_ids, list) or not all(
                isinstance(history_id, str) for history_id in raw_history_ids
            ):
                raise ValueError(
                    "conversation deletion payload has invalid history IDs"
                )
            history_ids = tuple(raw_history_ids)
            content_digest = payload.get("content_digest")
            if not isinstance(content_digest, str):
                raise TypeError(
                    "conversation deletion payload has an invalid content digest"
                )
            exact_preview = self._state.preview_conversation_deletion(
                ConversationDeletionScope.message(history_ids)
            )
            if (
                exact_preview.history_ids != history_ids
                or exact_preview.content_digest != content_digest
            ):
                raise ActionDispatcherError(
                    "conversation deletion preview no longer matches accessible history"
                )
            preview = ConversationDeletionPreview(
                scope=scope,
                messages=exact_preview.messages,
                content_digest=exact_preview.content_digest,
            )
            self._state.delete_conversation_history(
                preview,
                deletion_id=self._action.action_id,
                deleted_at=self._clock.now(),
            )
        except ActionDispatcherError:
            raise
        except StateStoreError as exc:
            raise ActionDispatcherError(
                f"conversation deletion could not be committed: {exc}",
                may_have_dispatched=exc.may_have_dispatched,
            ) from exc
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ActionDispatcherError(
                f"conversation deletion could not be committed: {exc}"
            ) from exc

    def cancel(self) -> ActionCancellationResult:
        with self._lock:
            if not self._started:
                self._cancelled = True
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)


@dataclass(frozen=True, slots=True)
class _RequestAdmission:
    """The durable request and session token produced by successful admission."""

    request: RequestState
    cancellation_token: CancellationToken


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    """Trust configuration for one dedicated messaging session."""

    operator_id: str
    session_id: str
    signing_secret: bytes
    max_text_length: int = 4096
    working_session_id: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.operator_id, "operator_id")
        require_non_empty(self.session_id, "session_id")
        if self.working_session_id is not None:
            require_non_empty(self.working_session_id, "working_session_id")
        if not isinstance(self.signing_secret, bytes) or not self.signing_secret:
            raise ValueError("signing_secret must be non-empty bytes")
        if self.max_text_length != 4096:
            raise ValueError("max_text_length is fixed at 4096 characters in V1")


__all__ = [name for name in globals() if not name.startswith("__")]
