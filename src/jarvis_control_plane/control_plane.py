"""Receiver and deterministic capability-broker path for ticket01/ticket03."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime
from threading import RLock

from .control_grammar import (
    ApprovalChoice,
    ControlCommand,
    ControlTransition,
    ControlTransitionKind,
    handle_message,
    parse_approval_choice,
    parse_control,
)
from .models import (
    AuditEvidence,
    ConversationDeletionPreview,
    ConversationDeletionScope,
    ConversationMessage,
    FrozenActionProposal,
    InboundMessage,
    OrchestrationRequest,
    OrchestrationResult,
    OutboundReply,
    ReceiveResult,
    RequestState,
    SignedInboundEvent,
)
from .ports import (
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
    DiagnosticTraceError,
    DurableStateStore,
    IdGenerator,
    ModelAvailabilityProvider,
    OrchestrationAdapter,
    OrchestrationAdapterError,
    OutboundConnector,
    OutboundConnectorError,
    StateStoreError,
    TraceCapacityError,
    TraceWriteError,
    require_non_empty,
)
from .sessions import (
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
    SessionConfig,
    SessionStoreError,
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
from .terminal_policy import (
    TerminalAction,
    TerminalDisposition,
    authorize_terminal_proposal,
    terminal_action_from_proposal,
)
from .traces import DiagnosticTraceRecorder

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
        except (StateStoreError, TypeError, ValueError, json.JSONDecodeError) as exc:
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


class DeterministicCapabilityBroker:
    """Reference monitor for the small request-to-reply tracer path."""

    def __init__(
        self,
        *,
        config: ControlPlaneConfig,
        state: DurableStateStore,
        audit: AuditBoundary,
        orchestration: OrchestrationAdapter,
        outbound: OutboundConnector,
        clock: Clock,
        ids: IdGenerator,
        trace: DiagnosticTraceRecorder,
        model_availability_provider: ModelAvailabilityProvider,
        working_sessions: WorkingSessionStore | None = None,
        action_dispatcher: ActionDispatcher | None = None,
        action_lifecycle: BoundActionLifecycle | None = None,
    ) -> None:
        if not isinstance(trace, DiagnosticTraceRecorder):
            raise TypeError(
                "trace must be an explicitly configured DiagnosticTraceRecorder"
            )
        self.config = config
        self.state = state
        self.audit = audit
        self.orchestration = orchestration
        self.outbound = outbound
        self.clock = clock
        self.ids = ids
        self.model_availability_provider = model_availability_provider
        self.working_sessions = working_sessions or InMemoryWorkingSessionStore()
        selected_dispatcher = action_dispatcher or _UnavailableActionDispatcher()
        if not isinstance(selected_dispatcher, ActionDispatcher):
            raise TypeError(
                "action_dispatcher must implement the prepared cancellable dispatch port"
            )
        self.action_dispatcher = selected_dispatcher
        self.action_lifecycle = action_lifecycle or _NoopActionLifecycle()
        existing_session = self.working_sessions.load()
        if existing_session is None:
            self.working_sessions.create(
                WorkingSession.initial(
                    config.operator_id,
                    clock,
                    session_id=(
                        config.working_session_id
                        or f"working-session-{config.session_id}"
                    ),
                    config=SessionConfig(operator_id=config.operator_id),
                )
            )
        else:
            self._invalidate_restart_work()
            self._record_pending_session_migration()
        # The recorder is a write-only capability backed by an isolated writer.
        # Never retain a readable diagnostic store on the broker graph.
        self._trace = trace
        self._dispatch_lock = RLock()
        self._session_lifecycle_lock = RLock()

    def handle(self, message: InboundMessage) -> ReceiveResult:
        """Accept one already-admitted message and drive its named lifecycle stages."""

        session = self._reconcile_inactivity()
        if session.pending_action is not None and session.pending_action.is_expired(
            self.clock
        ):
            return self._expire_pending_action(message, session)
        if session.pending_action is not None:
            choice = parse_approval_choice(message.text)
            if choice is not None:
                if not session.pending_action.is_confirmable:
                    return ReceiveResult(
                        status_code=202,
                        disposition="pending_presenting",
                        reason="proposal presentation is incomplete",
                    )
                current = self._current_working_session()
                if current.pending_action is None:
                    return ReceiveResult(
                        status_code=202,
                        disposition="pending_unavailable",
                    )
                if current.pending_action.is_expired(self.clock):
                    return self._expire_pending_action(message, current)
                return self._consume_pending_approval(message, choice)
        parsed = parse_control(message.text)
        if parsed.is_command and parsed.command is ControlCommand.HISTORY:
            if parsed.args and parsed.args[0] == "delete":
                return self._handle_history_deletion_control(message, parsed.args)
            return self._handle_history_control(message, parsed.args)
        try:
            model_availability = self._model_availability()
        except (TypeError, ValueError, RuntimeError) as exc:
            return ReceiveResult(
                status_code=503,
                disposition="model_availability_unavailable",
                reason=f"runtime model availability was unavailable: {exc}",
            )
        request_id = self.ids.new_id("request")
        session_transition = handle_message(
            session,
            message.text,
            now=self.clock,
            request_id=request_id,
            originating_message_id=message.message_id,
            phase="processing",
            model_availability=model_availability,
        )
        if session_transition.kind is not ControlTransitionKind.REQUEST_ACCEPTED:
            return self._handle_session_control(message, session, session_transition)

        admission = self._admit_request(
            message=message,
            session=session,
            session_transition=session_transition,
            request_id=request_id,
        )
        if isinstance(admission, ReceiveResult):
            return admission

        request = admission.request
        cancellation_token = admission.cancellation_token
        result = self._run_orchestration(
            message=message,
            request=request,
            cancellation_token=cancellation_token,
        )
        if isinstance(result, ReceiveResult):
            return result

        if not cancellation_token_is_current(
            self._current_working_session(), cancellation_token
        ):
            return self._late_result_result(request, message=message)

        if not self._selected_configuration_is_available(request):
            return self._configuration_unavailable_result(
                message=message,
                request=request,
                cancellation_token=cancellation_token,
            )

        if result.proposal is not None:
            try:
                action = self.freeze_action(self._bind_action_proposal(result.proposal))
                self._present_action(action, message)
                if action.policy_disposition in {
                    TerminalDisposition.SAFE_READ.value,
                    TerminalDisposition.EXACT_PERMISSION.value,
                }:
                    return self._auto_authorize_terminal_action(action, message)
            except (
                AuditWriteError,
                ActionDispatcherError,
                DiagnosticTraceError,
                InvariantViolation,
                OutboundConnectorError,
                SessionStoreError,
                ValueError,
            ) as exc:
                return self._finish_proposal_failure(
                    message=message,
                    request=request,
                    token=cancellation_token,
                    error=exc,
                )
            return ReceiveResult(
                status_code=202,
                disposition="pending_action",
                request=request,
                reason="exact action proposal is frozen pending operator approval",
            )

        return self._complete_outbound_reply(
            message=message,
            request=request,
            cancellation_token=cancellation_token,
            result=result,
        )

    @property
    def current_pending_action(self) -> PendingActionState | None:
        """Expose the current frozen action without exposing its payload in status."""

        return self._current_working_session().pending_action

    def _commit_session_with_audit(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        **audit_fields: object,
    ) -> None:
        """Use the session store's shared transaction for state and evidence."""

        evidence = self._audit_evidence(**audit_fields)  # ty:ignore[invalid-argument-type]
        self.working_sessions.compare_and_set_with_audit(
            expected, updated, audit=self.audit, evidence=evidence
        )

    def _record_pending_session_migration(self) -> None:
        """Record and clear a durable permission-shape migration marker."""

        for _ in range(3):
            session = self._current_working_session()
            count = session.legacy_permissions_invalidated
            if count == 0:
                return
            evidence = self._audit_evidence(
                kind="working_session_migration",
                event_id=None,
                request_id=None,
                outcome="migrated",
                actor="control_plane",
                operation_type="working_session",
                target_category="working_session",
                execution_status="recorded",
                details={
                    "count": str(count),
                    "state": "legacy_permissions_invalidated",
                },
            )
            updated = replace(session, legacy_permissions_invalidated=0)
            try:
                self.working_sessions.compare_and_set_with_audit(
                    session, updated, audit=self.audit, evidence=evidence
                )
                return
            except AuditWriteError:
                # Keep the marker durable so a later restart can retry the
                # required migration evidence without restoring authority.
                return
            except SessionStoreError:
                continue
        raise SessionStoreError("could not record working-session migration")

    def freeze_action(self, proposal: FrozenActionProposal) -> PendingActionState:
        """Persist one proposal whose exact payload can later be dispatched once."""

        for _ in range(3):
            session = self._current_working_session()
            request = session.active_request
            if request is None or request.request_id != proposal.request_id:
                raise InvariantViolation("proposal does not belong to the live request")
            policy_disposition: str | None = None
            if proposal.kind == "terminal":
                policy = authorize_terminal_proposal(
                    proposal, permissions=session.permissions
                )
                if policy.disposition is TerminalDisposition.HARD_PROHIBITED:
                    raise InvariantViolation("terminal action is hard-prohibited")
                policy_disposition = policy.disposition.value
            action = PendingActionState.from_proposal(
                proposal,
                session_id=session.session_id,
                created_at=self.clock,
                presentation_status=ProposalPresentationStatus.PRESENTING,
                policy_disposition=policy_disposition,
            )
            transition = install_pending_action(session, action, now=self.clock)
            try:
                self._commit_session_with_audit(
                    session,
                    transition.state,
                    kind="pending_action",
                    event_id=None,
                    request_id=proposal.request_id,
                    outcome="pending",
                    actor="control_plane",
                    operation_type="approval_gated_action",
                    target_category="pending_action",
                    details={"action": action.action_id, "state": "frozen"},
                )
            except SessionStoreError:
                continue
            return action
        raise SessionStoreError("pending action could not be frozen atomically")

    def _bind_action_proposal(
        self, proposal: FrozenActionProposal
    ) -> FrozenActionProposal:
        """Let the typed action surface add its current immutable binding."""

        bound = self.action_lifecycle.bind_proposal(proposal)
        if not isinstance(bound, FrozenActionProposal):
            raise InvariantViolation("action dispatcher returned an invalid proposal")
        return bound

    def _present_action(
        self, action: PendingActionState, message: InboundMessage
    ) -> None:
        """Send an all-or-nothing, durable proposal-envelope presentation."""

        fragments = tuple(
            action.preview[index : index + _PROPOSAL_FRAGMENT_PAYLOAD_CHARS]
            for index in range(0, len(action.preview), _PROPOSAL_FRAGMENT_PAYLOAD_CHARS)
        )
        if not fragments:
            raise InvariantViolation("frozen action preview must be non-blank")
        total = len(fragments)
        try:
            for number, fragment in enumerate(fragments, start=1):
                reply = OutboundReply(
                    reply_id=self.ids.new_id("proposal-fragment"),
                    request_id=action.request_id,
                    session_id=self.config.session_id,
                    recipient_id=message.chat_id,
                    body=(
                        f"Proposal {action.action_id} digest {action.digest} "
                        f"part {number}/{total} request_id={action.request_id}\n{fragment}"
                    ),
                )
                if len(reply.body) > _MAX_OUTBOUND_MESSAGE_CHARS:
                    raise InvariantViolation(
                        "proposal envelope exceeded outbound bound"
                    )
                outbound_id = self._send_presented_reply(reply, message=message)
                self._record_proposal_fragment(
                    action.action_id, number, total, outbound_id
                )

            prompt = OutboundReply(
                reply_id=self.ids.new_id("proposal-prompt"),
                request_id=action.request_id,
                session_id=self.config.session_id,
                recipient_id=message.chat_id,
                body=(
                    f"Proposal {action.action_id} digest {action.digest} "
                    "All proposal fragments were presented. "
                    f"{self._proposal_choices(action)} "
                    f"request_id={action.request_id}"
                ),
            )
            if len(prompt.body) > _MAX_OUTBOUND_MESSAGE_CHARS:
                raise InvariantViolation("proposal prompt exceeded outbound bound")
            self._send_presented_reply(prompt, message=message)
            self._mark_proposal_presented(action.action_id)
        except (
            DiagnosticTraceError,
            InvariantViolation,
            OutboundConnectorError,
            SessionStoreError,
            ValueError,
        ):
            self._invalidate_presenting_action(action.action_id)
            raise

    def _send_presented_reply(
        self, reply: OutboundReply, *, message: InboundMessage
    ) -> str:
        """Use the normal audit and trace admission boundary for one envelope send."""

        preflight = getattr(self.outbound, "preflight", None)
        if not callable(preflight):
            raise OutboundConnectorError(
                "outbound connector does not provide audit-safe preflight"
            )
        preflight(reply)
        self.audit.append_batch(
            (
                self._audit_evidence(
                    kind="outbound_attempt",
                    event_id=message.event_id,
                    request_id=reply.request_id,
                    message_id=message.message_id,
                    outcome="attempted",
                    actor="controlled_outbound",
                    operation_type="outbound_message",
                    target_category="operator_conversation",
                    details={
                        "channel": "controlled_outbound",
                        "destination": "configured_operator",
                    },
                ),
                self._audit_evidence(
                    kind="outbound_result",
                    event_id=message.event_id,
                    request_id=reply.request_id,
                    message_id=message.message_id,
                    outcome="pending",
                    actor="controlled_outbound",
                    operation_type="outbound_message",
                    target_category="operator_conversation",
                    execution_status="pending",
                    details={"channel": "controlled_outbound", "result": "pending"},
                ),
                self._audit_evidence(
                    kind="outbound_completion",
                    event_id=message.event_id,
                    request_id=reply.request_id,
                    message_id=message.message_id,
                    outcome="pending",
                    actor="controlled_outbound",
                    operation_type="outbound_message",
                    target_category="operator_conversation",
                    execution_status="pending",
                    details={"result": "pending"},
                ),
            )
        )
        self._reserve_outbound_history(reply, message=message)
        outbound_id: str | None = None

        def send() -> dict[str, str]:
            nonlocal outbound_id
            delivery = self.outbound.send(reply)
            outbound_id = getattr(delivery, "outbound_id", None)
            if getattr(delivery, "accepted", None) is not True or not isinstance(
                outbound_id, str
            ):
                raise OutboundConnectorError(
                    "proposal gateway outcome was unknown", may_have_sent=True
                )
            self._accept_outbound_history(reply)
            return {"outbound_id": outbound_id, "result": "accepted"}

        try:
            self._trace.execute(
                request_id=reply.request_id,
                operation_id=f"{reply.request_id}:connector:{reply.reply_id}",
                operation_type="connector",
                input_payload=reply,
                arguments={"operation": "send", "channel": "controlled_outbound"},
                telemetry={"phase": "proposal_presentation"},
                operation=send,
                result_limit_bytes=4_096,
                error_limit_bytes=8_192,
            )
        except (DiagnosticTraceError, OutboundConnectorError) as exc:
            result = "unknown" if getattr(exc, "may_have_sent", False) else "failed"
            self._best_effort_audit(
                kind="outbound_result",
                event_id=message.event_id,
                request_id=reply.request_id,
                message_id=message.message_id,
                outcome=result,
                actor="controlled_outbound",
                operation_type="outbound_message",
                target_category="operator_conversation",
                execution_status=result,
                details={"channel": "controlled_outbound", "result": result},
            )
            raise
        self._append_audit(
            kind="outbound_result",
            event_id=message.event_id,
            request_id=reply.request_id,
            message_id=message.message_id,
            outcome="accepted",
            actor="controlled_outbound",
            operation_type="outbound_message",
            target_category="operator_conversation",
            execution_status="accepted",
            details={"channel": "controlled_outbound", "result": "accepted"},
        )
        if outbound_id is None:
            raise InvariantViolation("accepted proposal delivery did not return an ID")
        return outbound_id

    def _record_proposal_fragment(
        self, action_id: str, number: int, total: int, outbound_id: str
    ) -> None:
        current = self._current_working_session()
        transition = record_proposal_fragment(
            current,
            action_id=action_id,
            number=number,
            total=total,
            outbound_id=outbound_id,
            now=self.clock,
        )
        self.working_sessions.compare_and_set(current, transition.state)

    def _mark_proposal_presented(self, action_id: str) -> None:
        current = self._current_working_session()
        transition = mark_proposal_presented(
            current, action_id=action_id, now=self.clock
        )
        self.working_sessions.compare_and_set(current, transition.state)

    def _invalidate_presenting_action(self, action_id: str) -> None:
        """An uncertain fragment result is terminal: never retry or keep payload."""

        for _ in range(3):
            current = self._current_working_session()
            action = current.pending_action
            if action is None or action.action_id != action_id:
                return
            transition = cancel_active_request(
                current, now=self.clock, reason="proposal_presentation_failed"
            )
            try:
                self.working_sessions.compare_and_set(current, transition.state)
                return
            except SessionStoreError:
                continue

    def _consume_pending_approval(
        self,
        message: InboundMessage,
        choice: object,
    ) -> ReceiveResult:
        """Consume approval and wait for dispatch outside the broker lock."""

        # Both human approval and deterministic auto-authorization use this one
        # lifecycle. The lock covers the durable approval boundary, while the
        # external dispatcher preparation runs unlocked so a stalled transport
        # cannot block /cancel or /new.
        with self._dispatch_lock:
            current = self._current_working_session()
            if current.pending_action is None:
                return ReceiveResult(
                    status_code=202,
                    disposition="pending_unavailable",
                )
            if current.pending_action.is_expired(self.clock):
                return self._expire_pending_action(message, current)
            approved_or_result = self._approve_pending_action(message, current, choice)
        if isinstance(approved_or_result, ReceiveResult):
            return approved_or_result
        prepared_or_result = self._prepare_approved_dispatch(
            message=message,
            action=approved_or_result.action,
            terminal=approved_or_result.terminal,
            permission_id=approved_or_result.permission_id,
        )
        if isinstance(prepared_or_result, ReceiveResult):
            return prepared_or_result
        if not self._dispatch_is_still_attempted(prepared_or_result.action.action_id):
            self._release_prepared_dispatch(
                prepared_or_result.action.action_id,
                handle=prepared_or_result.handle,
            )
            self._finalize_dispatch(prepared_or_result.action.action_id)
            return self._late_action_result(
                message,
                prepared_or_result,
                reason="worker registration completed after cancellation",
            )
        return self._run_prepared_action(message, prepared_or_result)

    def _approve_pending_action(
        self,
        message: InboundMessage,
        session: WorkingSession,
        choice: object,
    ) -> _ApprovedActionDispatch | ReceiveResult:
        """Own approval semantics before any external dispatcher is called."""

        action = session.pending_action
        request = session.active_request
        if action is None or request is None:
            return ReceiveResult(status_code=202, disposition="pending_unavailable")
        choice_value = getattr(choice, "value", choice)
        if choice_value == ApprovalChoice.REJECT.value:
            transition = reject_pending_action(session, now=self.clock)
            try:
                self._commit_session_with_audit(
                    session,
                    transition.state,
                    kind="pending_action",
                    event_id=message.event_id,
                    request_id=action.request_id,
                    message_id=message.message_id,
                    outcome="rejected",
                    actor="control_plane",
                    operation_type="approval_gated_action",
                    target_category="pending_action",
                    approval_decision="rejected",
                    details={"action": action.action_id, "state": "rejected"},
                )
            except (AuditWriteError, SessionStoreError) as exc:
                return ReceiveResult(
                    status_code=202,
                    disposition="audit_blocked",
                    reason=f"pending action rejection was not recorded: {exc}",
                )
            return ReceiveResult(status_code=202, disposition="action_rejected")

        invalidated = self._invalidate_changed_connector_action(
            message, session, action
        )
        if invalidated is not None:
            return invalidated

        transition = approve_pending_action(session, now=self.clock)
        approved_state = transition.state
        approval_decision = "approved"
        permission_id: str | None = None
        permission_to_create: CommandPermissionState | None = None
        terminal = None
        terminal_policy = None
        if action.kind == "terminal":
            frozen = FrozenActionProposal(
                action_id=action.action_id,
                request_id=action.request_id,
                kind=action.kind,
                preview=action.preview or "",
                payload=action.payload,
                digest=action.digest,
            )
            try:
                terminal = terminal_action_from_proposal(frozen)
                terminal_policy = authorize_terminal_proposal(
                    frozen, permissions=session.permissions
                )
            except (TypeError, ValueError):
                return ReceiveResult(
                    status_code=202,
                    disposition="pending_blocked",
                    reason="terminal proposal could not be revalidated before dispatch",
                )
            if action.policy_disposition == TerminalDisposition.EXACT_PERMISSION.value:
                if (
                    terminal_policy.disposition
                    is not TerminalDisposition.EXACT_PERMISSION
                    or terminal_policy.matched_permission_id is None
                ):
                    return self._reject_revoked_pending_action(
                        message,
                        session,
                        action,
                        permission_id=terminal_policy.matched_permission_id,
                    )
                permission_id = terminal_policy.matched_permission_id
                approved_state = replace(
                    approved_state,
                    permissions=tuple(
                        replace(permission, last_used_at=self.clock.now())
                        if permission.permission_id == permission_id
                        else permission
                        for permission in approved_state.permissions
                    ),
                )
        if choice_value in {
            ApprovalChoice.SESSION_PERMISSION.value,
            ApprovalChoice.PERSISTENT_PERMISSION.value,
        }:
            if action.policy_disposition not in {
                TerminalDisposition.PROTECTED_APPROVAL.value,
                TerminalDisposition.ORDINARY_APPROVAL.value,
            }:
                return ReceiveResult(status_code=202, disposition="pending_blocked")
            if terminal is None or terminal_policy is None:
                return ReceiveResult(status_code=202, disposition="pending_blocked")
            if terminal_policy.disposition not in {
                TerminalDisposition.PROTECTED_APPROVAL,
                TerminalDisposition.ORDINARY_APPROVAL,
            }:
                return ReceiveResult(status_code=202, disposition="pending_blocked")
            lifetime = (
                PermissionLifetime.SESSION
                if choice_value == ApprovalChoice.SESSION_PERMISSION.value
                else PermissionLifetime.PERSISTENT
            )
            permission_to_create = CommandPermissionState(
                permission_id=self.ids.new_id("permission"),
                lifetime=lifetime,
                identity=terminal.permission_identity,
                created_at=self.clock.now(),
                session_id=(
                    session.session_id
                    if lifetime is PermissionLifetime.SESSION
                    else None
                ),
                authorization_request_id=action.request_id,
                authorization_action_id=action.action_id,
                authorization_approval=choice_value,
            )
            permission_id = permission_to_create.permission_id
            approved_state = replace(
                approved_state,
                permissions=(*approved_state.permissions, permission_to_create),
            )
            approval_decision = choice_value
        try:
            details = {"action": action.action_id, "state": "approved"}
            if permission_id is not None:
                details["permission_id"] = permission_id
            evidence = self._audit_evidence(
                kind="pending_action",
                event_id=message.event_id,
                request_id=action.request_id,
                message_id=message.message_id,
                outcome="approved",
                actor="control_plane",
                operation_type="approval_gated_action",
                target_category="pending_action",
                approval_decision=approval_decision,
                details=details,
            )
            if permission_to_create is not None:
                approved_state = replace(
                    approved_state,
                    permissions=tuple(
                        replace(
                            permission,
                            authorization_audit_id=evidence.evidence_id,
                        )
                        if permission.permission_id
                        == permission_to_create.permission_id
                        else permission
                        for permission in approved_state.permissions
                    ),
                )
            self.working_sessions.compare_and_set_with_audit(
                session,
                approved_state,
                audit=self.audit,
                evidence=evidence,
            )
        except (AuditWriteError, SessionStoreError) as exc:
            self._close_unattempted_action(action.action_id)
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"pending action approval was not recorded: {exc}",
            )

        return _ApprovedActionDispatch(
            action=action,
            terminal=terminal,
            permission_id=permission_id,
        )

    def _prepare_approved_dispatch(
        self,
        *,
        message: InboundMessage,
        action: PendingActionState,
        terminal: TerminalAction | None,
        permission_id: str | None,
    ) -> _PreparedActionDispatch | ReceiveResult:
        """Prepare a dispatcher outside the broker's cancellation barrier."""

        with self._dispatch_lock:
            frozen_or_result = self._prepare_approved_dispatch_boundary(
                message=message,
                action=action,
                terminal=terminal,
                permission_id=permission_id,
            )
        if isinstance(frozen_or_result, ReceiveResult):
            return frozen_or_result
        if frozen_or_result.kind == "conversation_history_delete":
            return _PreparedActionDispatch(
                action=frozen_or_result,
                handle=_ConversationDeletionDispatch(
                    state=self.state,
                    action=frozen_or_result,
                    clock=self.clock,
                ),
            )
        try:
            handle = self.action_dispatcher.prepare(frozen_or_result)
            if not isinstance(handle, ActionDispatchHandle):
                raise ActionDispatcherError(
                    "action dispatcher returned an invalid dispatch handle"
                )
        except ActionDispatcherError as exc:
            terminal_status = _dispatch_failure_status(exc)
            if not self._finish_frozen_action(action.action_id, terminal_status):
                return ReceiveResult(
                    status_code=202,
                    disposition="action_dispatch_unknown",
                    reason="dispatcher failed and terminal state could not be persisted",
                )
            return ReceiveResult(
                status_code=202,
                disposition=_dispatch_disposition(terminal_status),
                reason=str(exc),
            )
        return _PreparedActionDispatch(action=frozen_or_result, handle=handle)

    def _prepare_approved_dispatch_boundary(
        self,
        *,
        message: InboundMessage,
        action: PendingActionState,
        terminal: TerminalAction | None,
        permission_id: str | None,
    ) -> FrozenActionProposal | ReceiveResult:
        """Run readiness and record the durable dispatch-attempt boundary."""

        dispatching = self._current_working_session()
        if permission_id is not None:
            active_permission = next(
                (
                    permission
                    for permission in dispatching.permissions
                    if permission.permission_id == permission_id
                    and permission.is_active
                    and terminal is not None
                    and permission.identity == terminal.permission_identity
                ),
                None,
            )
            if active_permission is None:
                self._close_unattempted_action(action.action_id)
                try:
                    self._append_audit(
                        kind="action_outcome",
                        event_id=message.event_id,
                        request_id=action.request_id,
                        message_id=message.message_id,
                        outcome="permission_revoked",
                        actor="control_plane",
                        operation_type="terminal_dispatch",
                        target_category="execution_host",
                        approval_decision="revoked",
                        execution_status="not_started",
                        details={"command": "terminal", "result": "not_started"},
                    )
                except AuditWriteError as exc:
                    return ReceiveResult(
                        status_code=202,
                        disposition="audit_blocked",
                        reason=(
                            "command permission was revoked before dispatch but "
                            f"the outcome was not recorded: {exc}"
                        ),
                    )
                return ReceiveResult(
                    status_code=202,
                    disposition="permission_revoked",
                    reason="the command permission was revoked before dispatch",
                )
        unavailable_host = self._unavailable_terminal_host(action, dispatching)
        if unavailable_host is not None:
            self._close_unattempted_action(action.action_id)
            try:
                self._append_audit(
                    kind="action_outcome",
                    event_id=message.event_id,
                    request_id=action.request_id,
                    message_id=message.message_id,
                    outcome="host_unavailable",
                    actor="control_plane",
                    operation_type="terminal_dispatch",
                    target_category="execution_host",
                    execution_status="not_started",
                    details={"command": "terminal", "result": "not_started"},
                )
            except AuditWriteError as exc:
                return ReceiveResult(
                    status_code=202,
                    disposition="audit_blocked",
                    reason=(
                        "selected execution host was unavailable but the "
                        f"outcome was not recorded: {exc}"
                    ),
                )
            return ReceiveResult(
                status_code=202,
                disposition="action_dispatch_unavailable",
                reason=(
                    f"selected execution host {unavailable_host} is not ready; "
                    "the action was not dispatched"
                ),
            )
        if action.kind == "conversation_history_delete":
            try:
                self._append_audit(
                    kind="conversation_history_deletion_attempt",
                    event_id=message.event_id,
                    request_id=action.request_id,
                    message_id=message.message_id,
                    outcome="attempted",
                    actor="control_plane",
                    operation_type="conversation_history_delete",
                    target_category="operator_conversation",
                    approval_decision="approved",
                    execution_status="attempted",
                    details={"action": action.action_id},
                )
            except AuditWriteError as exc:
                self._close_unattempted_action(action.action_id)
                return ReceiveResult(
                    status_code=202,
                    disposition="audit_blocked",
                    reason=f"conversation deletion was blocked by audit: {exc}",
                )
        try:
            attempted = mark_action_dispatch_attempted(
                dispatching, action_id=action.action_id, now=self.clock
            )
            self.working_sessions.compare_and_set(dispatching, attempted.state)
        except (InvariantViolation, SessionStoreError) as exc:
            return ReceiveResult(
                status_code=202,
                disposition="action_dispatch_not_started",
                reason=f"dispatch attempt was not durably recorded: {exc}",
            )
        record = next(
            item
            for item in attempted.state.action_outbox
            if item.action_id == action.action_id
        )
        frozen_dispatch = FrozenActionProposal(
            action_id=record.action_id,
            request_id=record.request_id,
            kind=record.kind,
            preview=record.preview or "",
            payload=record.payload or "",
            digest=record.digest,
        )
        return frozen_dispatch

    def _run_prepared_action(
        self, message: InboundMessage, prepared: _PreparedActionDispatch
    ) -> ReceiveResult:
        """Trace and await a prepared action after its cancellation barrier."""

        try:
            if prepared.action.kind in {"gmail_send", "gmail_reply"}:
                # Gmail owns the complete credential-bearing trace boundary.
                # Re-wrapping it here would reserve a second trace and would
                # change its definite pre-provider failure classification.
                prepared.handle.run()
            else:
                self._trace.execute(
                    request_id=prepared.action.request_id,
                    operation_id=f"{prepared.action.action_id}:dispatch",
                    operation_type="worker",
                    input_payload=prepared.action,
                    arguments={"operation": "dispatch", "kind": prepared.action.kind},
                    telemetry={"phase": "dispatch"},
                    operation=prepared.handle.run,
                    result_limit_bytes=2 * 1024 * 1024 + 16 * 1024,
                    error_limit_bytes=2 * 1024 * 1024 + 16 * 1024,
                )
        except (DiagnosticTraceError, ActionDispatcherError) as exc:
            self._release_prepared_dispatch(
                prepared.action.action_id,
                handle=prepared.handle,
            )
            terminal_status = _dispatch_failure_status(exc)
            if not self._finish_frozen_action(
                prepared.action.action_id, terminal_status
            ):
                return self._late_action_result(message, prepared, reason=str(exc))
            return ReceiveResult(
                status_code=202,
                disposition=_dispatch_disposition(terminal_status),
                reason=str(exc),
            )
        if not self._finish_frozen_action(
            prepared.action.action_id, DispatchStatus.COMPLETED
        ):
            return self._late_action_result(
                message,
                prepared,
                reason="action completed but terminal state was already closed",
            )
        return ReceiveResult(status_code=202, disposition="action_dispatched")

    def _dispatch_is_still_attempted(self, action_id: str) -> bool:
        """Check that cancellation did not close the edge while it prepared."""

        with self._dispatch_lock:
            current = self._current_working_session()
            record = next(
                (item for item in current.action_outbox if item.action_id == action_id),
                None,
            )
            return record is not None and record.status is DispatchStatus.ATTEMPTED

    def _release_prepared_dispatch(
        self,
        action_id: str,
        *,
        handle: ActionDispatchHandle | None = None,
    ) -> None:
        """Close a prepared edge when trace admission fails before dispatch."""

        try:
            if handle is not None and callable(getattr(handle, "cancel", None)):
                handle.cancel()  # type: ignore[attr-defined]
            else:
                self.action_dispatcher.cancel(action_id=action_id)
        except Exception:  # noqa: BLE001 - an unavailable edge is unknown
            # The durable action outcome below remains authoritative. A concrete
            # dispatcher must make cancellation bounded, but cleanup cannot
            # replace the outcome when that edge is already unavailable.
            return

    def _late_action_result(
        self,
        message: InboundMessage,
        prepared: _PreparedActionDispatch,
        *,
        reason: str,
    ) -> ReceiveResult:
        current = self._current_working_session()
        record = next(
            (
                item
                for item in current.action_outbox
                if item.action_id == prepared.action.action_id
            ),
            None,
        )
        if record is not None and record.status is DispatchStatus.CANCELLED:
            self._finalize_dispatch(prepared.action.action_id)
            self._best_effort_audit(
                kind="late_result_ignored",
                event_id=message.event_id,
                request_id=prepared.action.request_id,
                message_id=message.message_id,
                outcome="ignored",
                actor="control_plane",
                operation_type="terminal_dispatch",
                target_category="execution_host",
                details={},
            )
            return ReceiveResult(
                status_code=202,
                disposition="late_result_ignored",
                reason="worker result arrived after cancellation: " + reason,
            )
        if record is not None and record.status in {
            DispatchStatus.CANCELLING,
            DispatchStatus.UNKNOWN,
        }:
            if record.status is DispatchStatus.UNKNOWN:
                self._finalize_dispatch(prepared.action.action_id)
            return ReceiveResult(
                status_code=202,
                disposition="action_dispatch_unknown",
                reason="worker result arrived after an uncertain cancellation: "
                + reason,
            )
        if record is not None and not record.is_open:
            self._finalize_dispatch(prepared.action.action_id)
        return ReceiveResult(
            status_code=202,
            disposition="action_dispatch_unknown",
            reason=reason,
        )

    def _reject_revoked_pending_action(
        self,
        message: InboundMessage,
        session: WorkingSession,
        action: PendingActionState,
        *,
        permission_id: str | None,
    ) -> ReceiveResult:
        """Close an auto-authorized action whose exact rule was revoked."""

        transition = reject_pending_action(session, now=self.clock)
        details = {"action": action.action_id, "state": "rejected"}
        if permission_id is not None:
            details["permission_id"] = permission_id
        try:
            self._commit_session_with_audit(
                session,
                transition.state,
                kind="pending_action",
                event_id=message.event_id,
                request_id=action.request_id,
                message_id=message.message_id,
                outcome="rejected",
                actor="control_plane",
                operation_type="approval_gated_action",
                target_category="pending_action",
                approval_decision="revoked",
                details=details,
            )
        except (AuditWriteError, SessionStoreError) as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"revoked permission was not recorded: {exc}",
            )
        return ReceiveResult(
            status_code=202,
            disposition="permission_revoked",
            reason="the exact command permission was revoked before dispatch",
        )

    def _invalidate_changed_connector_action(
        self,
        message: InboundMessage,
        session: WorkingSession,
        action: PendingActionState,
    ) -> ReceiveResult | None:
        """Invalidate a pending connector action that no longer matches its binding."""

        frozen = FrozenActionProposal(
            action_id=action.action_id,
            request_id=action.request_id,
            kind=action.kind,
            preview=action.preview or "",
            payload=action.payload,
            digest=action.digest,
        )
        try:
            self.action_lifecycle.validate_pending_action(frozen)
        except ActionDispatcherError:
            transition = reject_pending_action(session, now=self.clock)
            try:
                self._commit_session_with_audit(
                    session,
                    transition.state,
                    kind="pending_action",
                    event_id=message.event_id,
                    request_id=action.request_id,
                    message_id=message.message_id,
                    outcome="rejected",
                    actor="control_plane",
                    operation_type="approval_gated_action",
                    target_category="pending_action",
                    approval_decision="invalidated",
                    details={"action": action.action_id, "state": "rejected"},
                )
            except (AuditWriteError, SessionStoreError) as exc:
                return ReceiveResult(
                    status_code=202,
                    disposition="audit_blocked",
                    reason=f"changed connector action was not invalidated: {exc}",
                )
            return ReceiveResult(
                status_code=202,
                disposition="action_invalidated",
                reason="connector connection changed after the proposal was frozen",
            )
        return None

    def _proposal_choices(self, action: PendingActionState) -> str:
        if action.policy_disposition in {
            TerminalDisposition.PROTECTED_APPROVAL.value,
            TerminalDisposition.ORDINARY_APPROVAL.value,
        }:
            return "1 Allow this time | 2 Allow for this session | 3 Allow every time | 4 Reject"
        if action.policy_disposition in {
            TerminalDisposition.SAFE_READ.value,
            TerminalDisposition.EXACT_PERMISSION.value,
        }:
            return "Automatically authorized by deterministic terminal policy"
        return "1 Allow this time | 4 Reject"

    def _auto_authorize_terminal_action(
        self, action: PendingActionState, message: InboundMessage
    ) -> ReceiveResult:
        """Consume safe-read or exact-permission authorization after presentation."""

        return self._consume_pending_approval(message, ApprovalChoice.APPROVE)

    def _finish_frozen_action(self, action_id: str, status: DispatchStatus) -> bool:
        with self._dispatch_lock:
            current = self._current_working_session()
            try:
                transition = complete_action_dispatch(
                    current, action_id=action_id, status=status, now=self.clock
                )
                self.working_sessions.compare_and_set(current, transition.state)
            except (InvariantViolation, SessionStoreError):
                return False
        self._finalize_dispatch(action_id)
        return True

    def _finalize_dispatch(self, action_id: str) -> None:
        """Run an optional transport retirement handshake after durable closure."""

        if not isinstance(self.action_dispatcher, ActionFinalizer):
            return
        try:
            self.action_dispatcher.finalize(action_id=action_id)
        except Exception:  # noqa: BLE001 - bounded retention is the fallback
            return

    @staticmethod
    def _unavailable_terminal_host(
        action: PendingActionState, session: WorkingSession
    ) -> str | None:
        """Return a terminal host that fails the last broker-side readiness gate."""

        if action.kind != "terminal":
            return None
        proposal = FrozenActionProposal(
            action_id=action.action_id,
            request_id=action.request_id,
            kind=action.kind,
            preview=action.preview or "",
            payload=action.payload,
            digest=action.digest,
        )
        host = terminal_action_from_proposal(proposal).host
        readiness = {
            "ubuntu": session.readiness.ubuntu,
            "windows": session.readiness.windows,
        }
        return None if readiness.get(host) == "ready" else host

    def _close_unattempted_action(self, action_id: str) -> None:
        """Fail closed after audit admission fails before the external attempt."""

        current = self._current_working_session()
        try:
            transition = complete_action_dispatch(
                current,
                action_id=action_id,
                status=DispatchStatus.NOT_STARTED,
                now=self.clock,
            )
            self.working_sessions.compare_and_set(current, transition.state)
        except (InvariantViolation, SessionStoreError):
            pass

    def _expire_pending_action(
        self, message: InboundMessage, session: WorkingSession
    ) -> ReceiveResult:
        transition = expire_pending_action(session, now=self.clock)
        try:
            self._commit_session_with_audit(
                session,
                transition.state,
                kind="pending_action",
                event_id=message.event_id,
                request_id=(
                    session.active_request.request_id
                    if session.active_request is not None
                    else None
                ),
                message_id=message.message_id,
                outcome="expired",
                actor="control_plane",
                operation_type="approval_gated_action",
                target_category="pending_action",
                details={"state": "expired"},
            )
        except (AuditWriteError, SessionStoreError) as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"pending expiry was not recorded: {exc}",
            )
        return ReceiveResult(status_code=202, disposition="pending_expired")

    def _finish_proposal_failure(
        self,
        *,
        message: InboundMessage,
        request: RequestState,
        token: CancellationToken,
        error: Exception,
    ) -> ReceiveResult:
        self._close_pending_proposal(token)
        return ReceiveResult(
            status_code=202,
            disposition="failed",
            request=request,
            reason=f"action proposal could not be frozen: {error}",
        )

    def _close_pending_proposal(self, token: CancellationToken) -> None:
        current = self._current_working_session()
        if not cancellation_token_is_current(current, token):
            return
        transition = cancel_active_request(
            current, now=self.clock, reason="proposal_audit_unavailable"
        )
        try:
            self.working_sessions.compare_and_set(current, transition.state)
        except SessionStoreError:
            pass

    def _invalidate_restart_work(self) -> None:
        """A recreated broker must never make a stale proposal executable."""

        for _ in range(3):
            session = self._current_working_session()
            transition = interrupt_for_restart(session, now=self.clock)
            if transition.kind is TransitionKind.NOOP:
                return
            try:
                self.working_sessions.compare_and_set(session, transition.state)
                return
            except SessionStoreError:
                continue
        raise SessionStoreError("could not invalidate work during restart")

    def _admit_request(
        self,
        *,
        message: InboundMessage,
        session: WorkingSession,
        session_transition: ControlTransition,
        request_id: str,
    ) -> _RequestAdmission | ReceiveResult:
        """Persist one request and claim the matching working-session generation."""

        request = RequestState(
            request_id=request_id,
            event_id=message.event_id,
            message_id=message.message_id,
            operator_id=self.config.operator_id,
            session_id=self.config.session_id,
            chat_id=message.chat_id,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
            status="accepted",
            phase="orchestration",
            model=session.model,
            reasoning=session.reasoning,
        )
        try:
            self.state.save_request(request)
            self._append_audit(
                kind="request_accepted",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome="accepted",
                actor="configured_operator",
                operation_type="request_lifecycle",
                target_category="control_plane",
                details={
                    "phase": "orchestration",
                    "model": request.model,
                    "reasoning": request.reasoning,
                },
            )
        except (StateStoreError, AuditWriteError) as admission_error:
            try:
                self.state.delete_request(request.request_id)
            except StateStoreError as transition_error:
                return ReceiveResult(
                    status_code=202,
                    disposition="failed",
                    request=request,
                    reason=(
                        "required audit evidence was unavailable and the blocked state "
                        "could not be rolled back: "
                        f"{transition_error}"
                    ),
                )
            blocked = replace(
                request,
                status="blocked",
                phase="audit_gate",
                outcome="audit_unavailable",
                error_code="audit_unavailable",
            )
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                request=blocked,
                reason=(
                    "required audit evidence was unavailable; the request admission "
                    f"was rolled back: {admission_error}"
                ),
            )

        try:
            self.working_sessions.compare_and_set(session, session_transition.state)
        except SessionStoreError as exc:
            try:
                self.state.delete_request(request.request_id)
            except StateStoreError:
                pass
            self._best_effort_audit(
                kind="working_session_conflict",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome="blocked",
                actor="control_plane",
                operation_type="request_lifecycle",
                target_category="working_session",
                details={},
            )
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                request=request,
                reason=f"working-session admission failed: {exc}",
            )

        cancellation_token = session_transition.cancellation_token
        assert cancellation_token is not None

        return _RequestAdmission(
            request=request,
            cancellation_token=cancellation_token,
        )

    def _run_orchestration(
        self,
        *,
        message: InboundMessage,
        request: RequestState,
        cancellation_token: CancellationToken,
    ) -> OrchestrationResult | ReceiveResult:
        """Execute and durably observe the controlled orchestration stage."""

        try:
            history = self.state.select_history_for_context(
                text=message.text,
                excluding_working_session_id=self.current_working_session_id,
                limit=5,
            )
            orchestration_request = OrchestrationRequest(
                state=request,
                text=message.text,
                history=history.messages,
            )
            result = self._trace.execute(
                request_id=request.request_id,
                operation_id=f"{request.request_id}:model",
                operation_type="model",
                input_payload=orchestration_request,
                arguments={
                    "adapter": type(self.orchestration).__name__,
                    "operation": "run",
                    "model": request.model,
                    "reasoning": request.reasoning,
                },
                telemetry={
                    "phase": "orchestration",
                    "model": request.model,
                    "reasoning": request.reasoning,
                },
                operation=lambda: self.orchestration.run(orchestration_request),
                result_limit_bytes=self.config.max_text_length * 8 + 4_096,
                error_limit_bytes=8_192,
            )
            selected_host = self._validate_orchestration_result(
                result, request_id=request.request_id
            )
            if selected_host is not None:
                self._record_execution_host(request, cancellation_token, selected_host)
            self._append_audit(
                kind="orchestration_result",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome=result.outcome,
                actor="controlled_orchestration",
                operation_type="orchestration",
                target_category="control_plane",
                details={
                    "adapter": result.adapter,
                    "model": request.model,
                    "reasoning": request.reasoning,
                },
            )
            if history.provenance_disclosure is not None:
                result = replace(
                    result,
                    reply_text=f"{history.provenance_disclosure}\n{result.reply_text}",
                )
        except (
            DiagnosticTraceError,
            OrchestrationAdapterError,
            AuditWriteError,
            SessionStoreError,
            StateStoreError,
            ValueError,
        ) as exc:
            trace_failed = isinstance(exc, (TraceCapacityError, TraceWriteError))
            failure_outcome = (
                "trace_unavailable" if trace_failed else "orchestration_failed"
            )
            failure_code = (
                "trace_capacity"
                if isinstance(exc, TraceCapacityError)
                else (
                    "trace_error"
                    if isinstance(exc, TraceWriteError)
                    else "orchestration_error"
                )
            )
            try:
                failed = self._transition(
                    request,
                    status="failed",
                    phase="orchestration",
                    outcome=failure_outcome,
                    error_code=failure_code,
                )
            except (StateStoreError, AuditWriteError) as transition_error:
                self._best_effort_audit(
                    kind="orchestration_result",
                    event_id=message.event_id,
                    request_id=request.request_id,
                    message_id=message.message_id,
                    outcome="failed",
                    actor="controlled_orchestration",
                    operation_type="orchestration",
                    target_category="control_plane",
                    details={"result": "failed", "state": "unavailable"},
                )
                self._finish_session_request(
                    cancellation_token,
                    outcome=failure_outcome,
                    message=message,
                )
                return ReceiveResult(
                    status_code=202,
                    disposition="failed",
                    request=request,
                    reason=(
                        f"{str(exc) or 'orchestration failed'}; the failure state "
                        "could not be persisted: "
                        f"{transition_error}"
                    ),
                )
            self._best_effort_audit(
                kind="orchestration_result",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome="failed",
                actor="controlled_orchestration",
                operation_type="orchestration",
                target_category="control_plane",
                details={"result": "failed"},
            )
            self._finish_session_request(
                cancellation_token,
                outcome=failure_outcome,
                message=message,
            )
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                request=failed,
                reason=str(exc) or "orchestration failed",
            )

        return result

    @staticmethod
    def _validate_orchestration_result(
        result: object, *, request_id: str
    ) -> str | None:
        """Validate model output before it can reach policy or outbound edges."""

        if not isinstance(result, OrchestrationResult):
            raise OrchestrationAdapterError(
                "orchestration adapter returned an untyped result"
            )
        if result.request_id != request_id:
            raise OrchestrationAdapterError("orchestration result correlation mismatch")
        if result.outcome != "completed":
            raise OrchestrationAdapterError(
                "orchestration adapter returned a non-completed outcome"
            )
        if result.adapter not in {"controlled", "agents_sdk_responses"}:
            raise OrchestrationAdapterError(
                "orchestration adapter is outside the configured boundary"
            )
        if result.proposal is not None and not isinstance(
            result.proposal, FrozenActionProposal
        ):
            raise OrchestrationAdapterError(
                "orchestration adapter returned an untyped proposal"
            )
        selected_host = result.execution_host
        if result.proposal is None or result.proposal.kind != "terminal":
            return selected_host
        try:
            terminal = terminal_action_from_proposal(result.proposal)
        except (TypeError, ValueError) as exc:
            raise OrchestrationAdapterError(
                "orchestration adapter returned a malformed terminal proposal"
            ) from exc
        if terminal.host not in {"ubuntu", "windows"}:
            raise OrchestrationAdapterError(
                "terminal proposal selected an unknown execution host"
            )
        if selected_host is not None and terminal.host != selected_host:
            raise OrchestrationAdapterError(
                "terminal proposal host does not match host selection"
            )
        return terminal.host

    def _record_execution_host(
        self,
        request: RequestState,
        token: CancellationToken,
        selected_host: str,
    ) -> None:
        """Persist the planner's closed host selection on the live request."""

        if selected_host not in {"ubuntu", "windows"}:
            raise OrchestrationAdapterError(
                "orchestration selected an unknown execution host"
            )
        for _ in range(3):
            current = self._current_working_session()
            if not cancellation_token_is_current(current, token):
                raise OrchestrationAdapterError(
                    "orchestration result no longer owns the working session"
                )
            active = current.active_request
            if active is None or active.request_id != request.request_id:
                raise OrchestrationAdapterError(
                    "orchestration result has no matching active request"
                )
            if active.execution_host is not None:
                if active.execution_host != selected_host:
                    raise OrchestrationAdapterError(
                        "execution host changed during orchestration"
                    )
                return
            updated = replace(
                current,
                active_request=replace(
                    active,
                    execution_host=selected_host,
                    updated_at=self.clock.now(),
                ),
            )
            try:
                self.working_sessions.compare_and_set(current, updated)
                return
            except SessionStoreError:
                continue
        raise SessionStoreError("execution host selection raced the working session")

    def _configuration_unavailable_result(
        self,
        *,
        message: InboundMessage,
        request: RequestState,
        cancellation_token: CancellationToken,
    ) -> ReceiveResult:
        """Finish the request without substituting an unavailable configuration."""

        self._finish_session_request(
            cancellation_token,
            outcome="model_availability_unavailable",
            message=message,
        )
        failed = self._transition(
            request,
            status="failed",
            phase="orchestration",
            outcome="orchestration_failed",
            error_code="model_availability_unavailable",
        )
        return ReceiveResult(
            status_code=202,
            disposition="model_availability_unavailable",
            request=failed,
            reason="selected model or reasoning became unavailable before dispatch",
        )

    def _complete_outbound_reply(
        self,
        *,
        message: InboundMessage,
        request: RequestState,
        cancellation_token: CancellationToken,
        result: OrchestrationResult,
    ) -> ReceiveResult:
        """Persist, dispatch, and reconcile the correlated outbound reply."""

        reply = OutboundReply(
            reply_id=self.ids.new_id("reply"),
            request_id=request.request_id,
            session_id=self.config.session_id,
            recipient_id=message.chat_id,
            body=_bounded_informational_reply(
                result.reply_text,
                request_id=request.request_id,
            ),
        )
        try:
            # This is the persistence gate for outbound dispatch.  A connector
            # must never be called until the durable state records that the
            # request is ready to reply.
            replying = self._transition(
                request,
                status="replying",
                phase="outbound",
                outcome="replying",
                error_code=None,
                reply_id=reply.reply_id,
            )
        except (StateStoreError, AuditWriteError) as exc:
            self._best_effort_audit(
                kind="outbound_completion",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome="not_sent",
                actor="controlled_outbound",
                operation_type="outbound_message",
                target_category="operator_conversation",
                execution_status="failed",
                details={"result": "not_sent", "state": "unavailable"},
            )
            self._finish_session_request(
                cancellation_token,
                outcome="outbound_failed",
                message=message,
            )
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                request=request,
                reason=f"could not persist replying state; outbound was not sent: {exc}",
            )

        side_effect_may_have_happened = False
        try:
            preflight = getattr(self.outbound, "preflight", None)
            if not callable(preflight):
                raise OutboundConnectorError(
                    "outbound connector does not provide audit-safe preflight"
                )
            preflight(reply)
            # The broker, rather than an adapter implementation, is the
            # reference-monitor gate.  A plain connector cannot dispatch until
            # the connector has guaranteed that the send is deterministic and
            # the bounded outbound-attempt/result admission is recorded.  The
            # result remains explicitly pending until the connector returns;
            # pre-dispatch evidence must never claim a successful send.
            # This batch is the durable dispatch-admission record.  It is
            # committed atomically before send(), so a later audit outage
            # cannot erase the fact that dispatch was admitted or justify an
            # automatic retry.  The result remains explicitly pending until
            # the connector returns; terminal evidence is an observation of
            # what happened after this point.
            self.audit.append_batch(
                (
                    self._audit_evidence(
                        kind="outbound_attempt",
                        event_id=message.event_id,
                        request_id=request.request_id,
                        message_id=message.message_id,
                        outcome="attempted",
                        actor="controlled_outbound",
                        operation_type="outbound_message",
                        target_category="operator_conversation",
                        details={
                            "channel": "controlled_outbound",
                            "destination": "configured_operator",
                        },
                    ),
                    self._audit_evidence(
                        kind="outbound_result",
                        event_id=message.event_id,
                        request_id=request.request_id,
                        message_id=message.message_id,
                        outcome="pending",
                        actor="controlled_outbound",
                        operation_type="outbound_message",
                        target_category="operator_conversation",
                        execution_status="pending",
                        details={
                            "channel": "controlled_outbound",
                            "result": "pending",
                        },
                    ),
                    # This immutable pending record is the terminal-evidence
                    # outbox.  It is admitted before dispatch so a storage
                    # failure cannot be discovered only after WhatsApp send.
                    self._audit_evidence(
                        kind="outbound_completion",
                        event_id=message.event_id,
                        request_id=request.request_id,
                        message_id=message.message_id,
                        outcome="pending",
                        actor="controlled_outbound",
                        operation_type="outbound_message",
                        target_category="operator_conversation",
                        execution_status="pending",
                        details={"result": "pending"},
                    ),
                )
            )
            self._reserve_outbound_history(reply, message=message)
            self._trace.execute(
                request_id=request.request_id,
                operation_id=f"{request.request_id}:connector:outbound",
                operation_type="connector",
                input_payload=reply,
                arguments={"operation": "send", "channel": "controlled_outbound"},
                telemetry={"phase": "outbound"},
                operation=lambda: self._send_request_and_finish(
                    reply,
                    cancellation_token,
                    outcome=result.outcome,
                    message=message,
                ),
                result_limit_bytes=4_096,
                error_limit_bytes=8_192,
            )
            side_effect_may_have_happened = True
            try:
                # Terminal evidence is an observation of the already-admitted
                # outbox.  It must never be a second dispatch gate: if this
                # append fails, the pending outbox record remains the durable
                # reconciliation point and the reply is reported unknown.
                self._append_audit(
                    kind="outbound_result",
                    event_id=message.event_id,
                    request_id=request.request_id,
                    message_id=message.message_id,
                    outcome="accepted",
                    actor="controlled_outbound",
                    operation_type="outbound_message",
                    target_category="operator_conversation",
                    execution_status="accepted",
                    details={"channel": "controlled_outbound", "result": "accepted"},
                )
            except AuditWriteError as observation_error:
                try:
                    unknown = self._transition(
                        replying,
                        status="unknown",
                        phase="outbound",
                        outcome="outbound_unknown",
                        error_code="audit_observation_error",
                        reply_id=reply.reply_id,
                        audit=False,
                    )
                except StateStoreError as transition_error:
                    return ReceiveResult(
                        status_code=202,
                        disposition="unknown",
                        request=replying,
                        reply=reply,
                        reason=(
                            "outbound reply was sent, but terminal audit evidence and "
                            f"unknown state could not be persisted: {transition_error}"
                        ),
                    )
                self._best_effort_audit(
                    kind="request_lifecycle",
                    event_id=message.event_id,
                    request_id=request.request_id,
                    message_id=message.message_id,
                    outcome="outbound_unknown",
                    actor="control_plane",
                    operation_type="request_lifecycle",
                    target_category="control_plane",
                    execution_status="unknown",
                    details={"phase": "outbound", "status": "unknown"},
                )
                return ReceiveResult(
                    status_code=202,
                    disposition="unknown",
                    request=unknown,
                    reply=reply,
                    reason=(
                        "outbound reply was sent; terminal audit evidence is pending "
                        f"reconciliation: {observation_error}"
                    ),
                )
            completed = self._transition(
                replying,
                status="completed",
                phase="completed",
                outcome="reply_sent",
                error_code=None,
                reply_id=reply.reply_id,
                audit=False,
            )
            self._best_effort_audit(
                kind="request_lifecycle",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome="reply_sent",
                actor="control_plane",
                operation_type="request_lifecycle",
                target_category="control_plane",
                execution_status="completed",
                details={"phase": "completed", "status": "completed"},
            )
        except _CancelledBeforeDispatch:
            self._best_effort_audit(
                kind="outbound_completion",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome="not_sent",
                actor="controlled_outbound",
                operation_type="outbound_message",
                target_category="operator_conversation",
                execution_status="failed",
                details={"result": "not_sent"},
            )
            return self._late_result_result(replying, message=message)
        except (
            DiagnosticTraceError,
            AuditWriteError,
            OutboundConnectorError,
            StateStoreError,
            ValueError,
        ) as exc:
            may_have_sent = (
                side_effect_may_have_happened
                or (isinstance(exc, OutboundConnectorError) and exc.may_have_sent)
                or (isinstance(exc, TraceWriteError) and exc.operation_started)
            )
            outcome = (
                "trace_unavailable"
                if isinstance(exc, TraceCapacityError)
                else "outbound_unknown"
                if may_have_sent
                else "outbound_failed"
            )
            error_code = (
                "trace_capacity"
                if isinstance(exc, TraceCapacityError)
                else "trace_error"
                if isinstance(exc, TraceWriteError)
                else "outbound_unknown"
                if may_have_sent
                else "outbound_error"
            )
            self._finish_session_request(
                cancellation_token,
                outcome=outcome,
                message=message,
            )
            if may_have_sent:
                self._best_effort_audit(
                    kind="outbound_result",
                    event_id=message.event_id,
                    request_id=request.request_id,
                    message_id=message.message_id,
                    outcome="unknown",
                    actor="controlled_outbound",
                    operation_type="outbound_message",
                    target_category="operator_conversation",
                    execution_status="unknown",
                    details={"channel": "controlled_outbound", "result": "unknown"},
                )
            else:
                self._best_effort_audit(
                    kind="outbound_result",
                    event_id=message.event_id,
                    request_id=request.request_id,
                    message_id=message.message_id,
                    outcome="failed",
                    actor="controlled_outbound",
                    operation_type="outbound_message",
                    target_category="operator_conversation",
                    execution_status="failed",
                    details={"channel": "controlled_outbound", "result": "failed"},
                )
            try:
                failed = self._transition(
                    replying,
                    status=("unknown" if may_have_sent else "failed"),
                    phase="outbound",
                    outcome=outcome,
                    error_code=error_code,
                    audit=not may_have_sent,
                )
            except (StateStoreError, AuditWriteError) as transition_error:
                if not may_have_sent:
                    self._best_effort_audit(
                        kind="outbound_completion",
                        event_id=message.event_id,
                        request_id=request.request_id,
                        message_id=message.message_id,
                        outcome=outcome,
                        actor="controlled_outbound",
                        operation_type="outbound_message",
                        target_category="operator_conversation",
                        execution_status=(
                            "unknown" if outcome == "outbound_unknown" else "failed"
                        ),
                        details={"result": outcome, "state": "unavailable"},
                    )
                reason = (
                    f"{str(exc) or 'outbound connector failed'}; the {outcome} "
                    "state could not be persisted: "
                    f"{transition_error}"
                )
                if may_have_sent and isinstance(exc, StateStoreError):
                    reason = (
                        "outbound reply was accepted, but durable completion state "
                        f"could not be persisted: {exc}; {reason}"
                    )
                return ReceiveResult(
                    status_code=202,
                    disposition="unknown" if may_have_sent else "failed",
                    request=replying,
                    reply=reply if may_have_sent else None,
                    reason=reason,
                )
            if may_have_sent:
                self._best_effort_audit(
                    kind="request_lifecycle",
                    event_id=message.event_id,
                    request_id=request.request_id,
                    message_id=message.message_id,
                    outcome="outbound_unknown",
                    actor="control_plane",
                    operation_type="request_lifecycle",
                    target_category="control_plane",
                    execution_status="unknown",
                    details={"phase": "outbound", "status": "unknown"},
                )
            else:
                self._best_effort_audit(
                    kind="outbound_completion",
                    event_id=message.event_id,
                    request_id=request.request_id,
                    message_id=message.message_id,
                    outcome=outcome,
                    actor="controlled_outbound",
                    operation_type="outbound_message",
                    target_category="operator_conversation",
                    execution_status=(
                        "unknown" if outcome == "outbound_unknown" else "failed"
                    ),
                    details={"result": outcome},
                )
            return ReceiveResult(
                status_code=202,
                disposition="unknown" if may_have_sent else "failed",
                request=failed,
                reply=reply if may_have_sent else None,
                reason=str(exc) or "outbound connector failed",
            )

        return ReceiveResult(
            status_code=202,
            disposition="completed",
            request=completed,
            reply=reply,
        )

    @property
    def current_working_session_id(self) -> str:
        """Expose only the current conversation boundary to ingress admission."""

        return self._reconcile_inactivity().session_id

    def _current_working_session(self) -> WorkingSession:
        session = self.working_sessions.load()
        if session is None:
            raise SessionStoreError("working session is unavailable")
        return session

    def _reconcile_inactivity(self) -> WorkingSession:
        """Advance an idle working session before it receives another message."""

        with self._session_lifecycle_lock:
            for _ in range(3):
                session = self._current_working_session()
                transition = expire_inactive_session(session, now=self.clock)
                if transition.kind is not TransitionKind.SESSION_EXPIRED:
                    return session
                self._append_audit(
                    kind="working_session_expired",
                    event_id=None,
                    request_id=None,
                    outcome="expired",
                    actor="control_plane",
                    operation_type="working_session_lifecycle",
                    target_category="working_session",
                    details={},
                )
                try:
                    self.working_sessions.compare_and_set(session, transition.state)
                    return transition.state
                except SessionStoreError:
                    continue
        raise SessionStoreError("working-session inactivity reconciliation raced")

    def _model_availability(self) -> ModelAvailability:
        try:
            availability = self.model_availability_provider.current()
        except Exception as exc:
            raise RuntimeError("availability provider check failed") from exc
        if not isinstance(availability, ModelAvailability):
            raise TypeError("model availability provider returned an invalid value")
        return availability

    def _handle_history_deletion_control(
        self,
        message: InboundMessage,
        args: tuple[str, ...],
    ) -> ReceiveResult:
        """Preview one exact history scope, then freeze it behind approval."""

        try:
            self._append_audit(
                kind="conversation_history_deletion_preview",
                event_id=message.event_id,
                request_id=None,
                message_id=message.message_id,
                outcome="requested",
                actor="configured_operator",
                operation_type="conversation_history_delete",
                target_category="operator_conversation",
                details={"scope": args[1]},
            )
        except AuditWriteError as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"conversation deletion preview was blocked by audit: {exc}",
            )

        current = self._current_working_session()
        if current.pending_action is not None:
            return self._dispatch_history_text(
                message,
                "A pending action already owns this working session. Approve, reject, "
                "or cancel it before requesting deletion.",
                disposition="pending_blocked",
            )
        if current.active_request is not None or any(
            record.is_open for record in current.action_outbox
        ):
            return self._dispatch_history_text(
                message,
                "A request is still active. Use /status or /cancel before requesting "
                "conversation deletion.",
                disposition="busy_refused",
            )

        try:
            scope = self._parse_history_deletion_scope(args)
            preview = self.state.preview_conversation_deletion(scope)
        except (StateStoreError, TypeError, ValueError) as exc:
            return self._dispatch_history_text(
                message,
                f"Conversation deletion preview failed: {type(exc).__name__}.",
                disposition="history_delete_failed",
            )
        if not preview.messages:
            return self._dispatch_history_text(
                message,
                "No accessible conversation messages matched that deletion scope.",
                disposition="history_delete_empty",
            )

        request_id = self.ids.new_id("request")
        session_transition = accept_request(
            current,
            now=self.clock,
            request_id=request_id,
            originating_message_id=message.message_id,
            phase="processing",
        )
        if session_transition.kind is not TransitionKind.REQUEST_ACCEPTED:
            return self._dispatch_history_text(
                message,
                "Conversation deletion could not start because the working session "
                "changed. Use /status and try again.",
                disposition="history_delete_busy",
            )
        admission = self._admit_request(
            message=message,
            session=current,
            session_transition=ControlTransition(
                state=session_transition.state,
                parsed=parse_control(message.text),
                kind=ControlTransitionKind.REQUEST_ACCEPTED,
                cancellation_token=session_transition.cancellation_token,
            ),
            request_id=request_id,
        )
        if isinstance(admission, ReceiveResult):
            return admission

        proposal = FrozenActionProposal.create(
            action_id=self.ids.new_id("history-delete"),
            request_id=admission.request.request_id,
            kind="conversation_history_delete",
            preview=self._render_history_deletion_preview(preview),
            payload=_deletion_payload(preview),
        )
        try:
            action = self.freeze_action(proposal)
            self._present_action(action, message)
        except (
            AuditWriteError,
            ActionDispatcherError,
            DiagnosticTraceError,
            InvariantViolation,
            OutboundConnectorError,
            SessionStoreError,
            StateStoreError,
            ValueError,
        ) as exc:
            return self._finish_proposal_failure(
                message=message,
                request=admission.request,
                token=admission.cancellation_token,
                error=exc,
            )
        return ReceiveResult(
            status_code=202,
            disposition="pending_action",
            request=admission.request,
            reason="exact conversation deletion proposal is frozen pending approval",
        )

    @staticmethod
    def _parse_history_deletion_scope(
        args: tuple[str, ...],
    ) -> ConversationDeletionScope:
        if len(args) < 3 or args[0] != "delete":
            raise ValueError("history deletion arguments are malformed")
        selector_type = args[1]
        if selector_type == "message" and len(args) == 3:
            return ConversationDeletionScope.message(args[2])
        if selector_type == "conversation" and len(args) == 3:
            return ConversationDeletionScope.conversation(args[2])
        if selector_type in {"date", "range"} and len(args) == 4:
            try:
                start = date.fromisoformat(args[2])
                end = date.fromisoformat(args[3])
            except ValueError as exc:
                raise ValueError("history deletion dates must be ISO dates") from exc
            return ConversationDeletionScope.date_range(start, end)
        raise ValueError("history deletion arguments are malformed")

    @staticmethod
    def _render_history_deletion_preview(
        preview: ConversationDeletionPreview,
    ) -> str:
        lines = [
            "Conversation-history deletion preview",
            f"Scope: {preview.scope.describe()}",
            f"Messages: {preview.count}",
            f"Selection digest: {preview.content_digest}",
            "Selected records:",
        ]
        lines.extend(
            (
                f"{message.history_id} | {message.occurred_at.isoformat()} | "
                f"{message.direction} | conversation {message.working_session_id} | "
                f"request {message.request_id or 'none'}"
            )
            for message in preview.messages
        )
        return "\n".join(lines)

    def _handle_history_control(
        self,
        message: InboundMessage,
        args: tuple[str, ...],
    ) -> ReceiveResult:
        """Serve bounded history reads only for the admitted operator command.

        Search returns opaque record pointers.  Content, including
        credential-like content, is released only by the exact message-id
        selector used by ``inspect`` and ``export``.
        """

        operation = args[0]
        try:
            self._append_audit(
                kind=f"conversation_history_{operation}",
                event_id=message.event_id,
                request_id=None,
                message_id=message.message_id,
                outcome="requested",
                actor="configured_operator",
                operation_type="conversation_history",
                target_category="operator_conversation",
                details={},
            )
            if operation == "search":
                matches = self.state.search_conversation_messages(
                    text=" ".join(args[1:]), limit=20
                )
                return self._dispatch_history_text(
                    message,
                    self._render_history_search(matches),
                    disposition="history_search",
                )
            if operation == "conversation":
                matches = self.state.search_conversation_messages(
                    working_session_id=args[1], limit=20
                )
                return self._dispatch_history_text(
                    message,
                    self._render_history_search(matches),
                    disposition="history_conversation",
                )

            selected = self.state.search_conversation_messages(
                history_ids=(args[1],), limit=1
            )
            if len(selected) != 1:
                return self._dispatch_history_text(
                    message,
                    "No accessible conversation message has that exact history ID.",
                    disposition=f"history_{operation}",
                )
            # The adapter's canonical export is the exact inspection payload:
            # it retains every selected field and is split only at the delivery
            # envelope, never truncated or redacted.
            payload = self.state.export_conversation_messages(history_ids=(args[1],))
        except AuditWriteError as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"conversation-history read was blocked by audit: {exc}",
            )
        except (StateStoreError, ValueError) as exc:
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                reason=f"conversation-history read failed: {exc}",
            )
        return self._dispatch_history_export(
            message,
            payload,
            disposition=f"history_{operation}",
        )

    @staticmethod
    def _render_history_search(messages: tuple[ConversationMessage, ...]) -> str:
        if not messages:
            return "No accessible conversation messages matched."
        return "\n".join(
            (
                "Conversation-history matches (inspect or export by exact history ID):",
                *(
                    f"{item.history_id} | conversation {item.working_session_id} | "
                    f"{item.direction} | {item.occurred_at.isoformat()} | "
                    f"request {item.request_id or 'none'}"
                    for item in messages
                ),
            )
        )

    def _dispatch_history_text(
        self,
        message: InboundMessage,
        text: str,
        *,
        disposition: str,
    ) -> ReceiveResult:
        control_id = self.ids.new_id("control")
        body = _bounded_informational_reply(text, request_id=control_id)
        result = self._dispatch_control_text(message, body=body, control_id=control_id)
        if result.disposition != "control_sent":
            return result
        return replace(result, disposition=disposition)

    def _dispatch_history_export(
        self,
        message: InboundMessage,
        payload: str,
        *,
        disposition: str,
    ) -> ReceiveResult:
        control_id = self.ids.new_id("control")
        fragments = tuple(
            payload[index : index + _PROPOSAL_FRAGMENT_PAYLOAD_CHARS]
            for index in range(0, len(payload), _PROPOSAL_FRAGMENT_PAYLOAD_CHARS)
        )
        if not fragments:
            raise InvariantViolation("history export payload must be non-blank")
        result: ReceiveResult | None = None
        for number, fragment in enumerate(fragments, start=1):
            body = (
                f"Conversation-history export part {number}/{len(fragments)} "
                f"request_id={control_id}\n{fragment}"
            )
            result = self._dispatch_control_text(
                message, body=body, control_id=control_id
            )
            if result.disposition != "control_sent":
                return result
        assert result is not None
        return replace(result, disposition=disposition)

    def _selected_configuration_is_available(self, request: RequestState) -> bool:
        try:
            return self._model_availability().supports(
                model=request.model,
                reasoning=request.reasoning,
            )
        except (TypeError, ValueError, RuntimeError):
            return False

    def _handle_session_control(
        self,
        message: InboundMessage,
        expected: WorkingSession,
        transition: ControlTransition,
    ) -> ReceiveResult:
        guard = self._dispatch_lock if transition.parsed.is_command else nullcontext()
        with guard:
            if transition.parsed.is_command:
                expected = self._current_working_session()
                try:
                    model_availability = self._model_availability()
                except (TypeError, ValueError, RuntimeError) as exc:
                    return ReceiveResult(
                        status_code=503,
                        disposition="model_availability_unavailable",
                        reason=f"runtime model availability was unavailable: {exc}",
                    )
                transition = handle_message(
                    expected,
                    message.text,
                    now=self.clock,
                    model_availability=model_availability,
                )
            return self._apply_session_control(message, expected, transition)

    def _apply_session_control(
        self,
        message: InboundMessage,
        expected: WorkingSession,
        transition: ControlTransition,
    ) -> ReceiveResult:
        dispatches_to_cancel = tuple(
            (record.action_id, record.kind)
            for record in expected.action_outbox
            if record.is_open
        )
        audit_kind = {
            ControlTransitionKind.STATUS: "working_session_status_viewed",
            ControlTransitionKind.CANCELLED: "working_session_cancelled",
            ControlTransitionKind.NOTHING_TO_CANCEL: "working_session_cancel_noop",
            ControlTransitionKind.NEW_SESSION: "working_session_replaced",
            ControlTransitionKind.BUSY_REFUSED: "request_refused_busy",
            ControlTransitionKind.PENDING_BLOCKED: "request_refused_pending",
            ControlTransitionKind.EMPTY: "empty_control_ignored",
            ControlTransitionKind.MALFORMED_COMMAND: "malformed_control_ignored",
            ControlTransitionKind.UNKNOWN_COMMAND: "unknown_control_ignored",
        }.get(transition.kind, "working_session_control")
        try:
            self._append_audit(
                kind=audit_kind,
                event_id=message.event_id,
                request_id=(
                    expected.active_request.request_id
                    if expected.active_request is not None
                    else None
                ),
                message_id=message.message_id,
                outcome=transition.kind.value,
                actor="configured_operator",
                operation_type="working_session_control",
                target_category="working_session",
                details={},
            )
        except AuditWriteError as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"working-session control was blocked by audit: {exc}",
            )

        if transition.state != expected:
            try:
                self.working_sessions.compare_and_set(expected, transition.state)
            except SessionStoreError as exc:
                return ReceiveResult(
                    status_code=202,
                    disposition="failed",
                    reason=f"working-session control lost a concurrent race: {exc}",
                )
        cancellation_outcomes: tuple[_CancellationOutcome, ...] = ()
        if transition.kind in {
            ControlTransitionKind.CANCELLED,
            ControlTransitionKind.NEW_SESSION,
        }:
            cancellation_outcomes = self._cancel_dispatches(dispatches_to_cancel)
            if transition.kind in {
                ControlTransitionKind.CANCELLED,
                ControlTransitionKind.NEW_SESSION,
            }:
                try:
                    for cancellation in cancellation_outcomes:
                        current = self._current_working_session()
                        reconciliation = reconcile_action_cancellation(
                            current,
                            action_id=cancellation.action_id,
                            status=cancellation.durable_status,
                            now=self.clock,
                        )
                        self.working_sessions.compare_and_set(
                            current, reconciliation.state
                        )
                except (InvariantViolation, SessionStoreError) as exc:
                    self._best_effort_audit(
                        kind="action_cancellation_reconciliation_failed",
                        event_id=message.event_id,
                        request_id=(
                            expected.active_request.request_id
                            if expected.active_request is not None
                            else None
                        ),
                        message_id=message.message_id,
                        outcome="unknown",
                        actor="control_plane",
                        operation_type="action_cancellation",
                        target_category="side_effect",
                        details={"reason": type(exc).__name__},
                    )
                    return ReceiveResult(
                        status_code=202,
                        disposition="cancellation_unknown",
                        reason=(
                            "cancellation was requested but its durable outcome "
                            "could not be reconciled"
                        ),
                    )
            try:
                self._append_cancellation_audit(
                    message, expected, cancellation_outcomes
                )
            except AuditWriteError as exc:
                return ReceiveResult(
                    status_code=202,
                    disposition="audit_blocked",
                    reason=f"cancellation outcome was not recorded: {exc}",
                )

        if transition.reply is None:
            return ReceiveResult(status_code=202, disposition=transition.kind.value)
        if cancellation_outcomes:
            transition = replace(
                transition,
                reply=self._cancellation_reply(transition, cancellation_outcomes),
            )
        return self._dispatch_control_reply(message, transition)

    def _cancel_dispatches(
        self, action_refs: tuple[tuple[str, str], ...]
    ) -> tuple[_CancellationOutcome, ...]:
        """Close every side-effect edge and preserve cancellation uncertainty."""

        results: list[_CancellationOutcome] = []
        for action_id, kind in action_refs:
            try:
                result = self.action_dispatcher.cancel(action_id=action_id)
                if not isinstance(result, ActionCancellationResult):
                    raise TypeError("action cancellation returned an invalid result")
            except Exception:  # noqa: BLE001 - an unavailable edge is unknown
                result = ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
            durable_status = (
                DispatchStatus.CANCELLED
                if result.status
                in {
                    ActionCancellationStatus.NOT_STARTED,
                    ActionCancellationStatus.STOPPED,
                }
                else DispatchStatus.UNKNOWN
            )
            results.append(
                _CancellationOutcome(
                    action_id=action_id,
                    kind=kind,
                    result=result,
                    durable_status=durable_status,
                )
            )
        return tuple(results)

    def _append_cancellation_audit(
        self,
        message: InboundMessage,
        expected: WorkingSession,
        outcomes: tuple[_CancellationOutcome, ...],
    ) -> None:
        """Record only bounded identifiers and cancellation outcomes."""

        request_id = (
            expected.active_request.request_id
            if expected.active_request is not None
            else None
        )
        for outcome in outcomes:
            self._append_audit(
                kind="action_cancellation",
                event_id=message.event_id,
                request_id=request_id,
                message_id=message.message_id,
                outcome=outcome.durable_status.value,
                actor="control_plane",
                operation_type="action_cancellation",
                target_category=(
                    "execution_host" if outcome.kind == "terminal" else "side_effect"
                ),
                execution_status=outcome.result.status.value,
                details={
                    "action": outcome.action_id,
                    "dispatch_state": outcome.durable_status.value,
                    "execution_status": outcome.result.status.value,
                },
            )

    @staticmethod
    def _cancellation_reply(
        transition: ControlTransition,
        outcomes: tuple[_CancellationOutcome, ...],
    ) -> str:
        """Tell the operator when any external action remains uncertain."""

        if any(
            outcome.durable_status is DispatchStatus.UNKNOWN for outcome in outcomes
        ):
            if transition.kind is ControlTransitionKind.NEW_SESSION:
                return (
                    "Started a clean session, but one or more previous external "
                    "actions remain of unknown outcome. No retry will be attempted."
                )
            return (
                "Cancellation was accepted, but one or more external actions have "
                "an unknown outcome. No retry will be attempted."
            )
        if transition.kind is ControlTransitionKind.NEW_SESSION:
            return (
                "Started a clean session. Previous work was stopped or confirmed "
                "not started; no action will resume."
            )
        return (
            "Cancelled the active request and invalidated its pending action. "
            "External actions were stopped or confirmed not started."
        )

    def _dispatch_control_reply(
        self,
        message: InboundMessage,
        transition: ControlTransition,
    ) -> ReceiveResult:
        control_id = self.ids.new_id("control")
        return self._dispatch_control_text(
            message,
            body=_bounded_informational_reply(
                transition.reply or "Control completed.", request_id=control_id
            ),
            control_id=control_id,
            disposition=transition.kind.value,
        )

    def _dispatch_control_text(
        self,
        message: InboundMessage,
        *,
        body: str,
        control_id: str,
        disposition: str = "control_sent",
    ) -> ReceiveResult:
        reply = OutboundReply(
            reply_id=self.ids.new_id("reply"),
            request_id=control_id,
            session_id=self.config.session_id,
            recipient_id=message.chat_id,
            body=body,
        )
        try:
            self.outbound.preflight(reply)
            self.audit.append_batch(
                (
                    self._audit_evidence(
                        kind="outbound_attempt",
                        event_id=message.event_id,
                        request_id=control_id,
                        message_id=message.message_id,
                        outcome="attempted",
                        actor="controlled_outbound",
                        operation_type="outbound_message",
                        target_category="operator_conversation",
                        details={"channel": "controlled_outbound"},
                    ),
                    self._audit_evidence(
                        kind="outbound_result",
                        event_id=message.event_id,
                        request_id=control_id,
                        message_id=message.message_id,
                        outcome="pending",
                        actor="controlled_outbound",
                        operation_type="outbound_message",
                        target_category="operator_conversation",
                        execution_status="pending",
                        details={"result": "pending"},
                    ),
                    self._audit_evidence(
                        kind="outbound_completion",
                        event_id=message.event_id,
                        request_id=control_id,
                        message_id=message.message_id,
                        outcome="pending",
                        actor="controlled_outbound",
                        operation_type="outbound_message",
                        target_category="operator_conversation",
                        execution_status="pending",
                        details={"result": "pending"},
                    ),
                )
            )
            # The private outbox record is the dispatch admission gate.  It must be
            # durable before tracing enters the connector operation so a local
            # outbox failure is a definite no-send result, not an ambiguous
            # connector outcome.
            self._reserve_outbound_history(reply, message=message)
            self._trace.execute(
                request_id=control_id,
                operation_id=f"{control_id}:connector:outbound",
                operation_type="connector",
                input_payload=reply,
                arguments={"operation": "send", "channel": "controlled_outbound"},
                telemetry={"phase": "control_reply"},
                operation=lambda: self._send_and_confirm(reply, message=message),
                result_limit_bytes=4_096,
                error_limit_bytes=8_192,
            )
            self._append_audit(
                kind="outbound_result",
                event_id=message.event_id,
                request_id=control_id,
                message_id=message.message_id,
                outcome="accepted",
                actor="controlled_outbound",
                operation_type="outbound_message",
                target_category="operator_conversation",
                execution_status="accepted",
                details={"result": "accepted"},
            )
        except (
            DiagnosticTraceError,
            AuditWriteError,
            OutboundConnectorError,
            ValueError,
        ) as exc:
            may_have_sent = (
                isinstance(exc, OutboundConnectorError) and exc.may_have_sent
            ) or (isinstance(exc, TraceWriteError) and exc.operation_started)
            return ReceiveResult(
                status_code=202,
                disposition="unknown" if may_have_sent else "failed",
                reply=reply if may_have_sent else None,
                reason=str(exc) or "control reply failed",
            )
        return ReceiveResult(
            status_code=202,
            disposition=disposition,
            reply=reply,
        )

    def _finish_session_request(
        self,
        token: CancellationToken,
        *,
        outcome: str,
        message: InboundMessage,
    ) -> bool:
        for _ in range(3):
            current = self._current_working_session()
            transition = apply_request_result(
                current,
                token,
                RequestResult(
                    request_id=token.request_id,
                    generation=token.generation,
                    outcome=outcome,
                ),
                now=self.clock,
            )
            if transition.kind is TransitionKind.LATE_RESULT_IGNORED:
                self._best_effort_audit(
                    kind="late_result_ignored",
                    event_id=message.event_id,
                    request_id=token.request_id,
                    message_id=message.message_id,
                    outcome="ignored",
                    actor="control_plane",
                    operation_type="request_lifecycle",
                    target_category="working_session",
                    details={},
                )
                return False
            try:
                self.working_sessions.compare_and_set(current, transition.state)
                return True
            except SessionStoreError:
                continue
        return False

    def _send_and_confirm(
        self, reply: OutboundReply, *, message: InboundMessage
    ) -> dict[str, str]:
        self.outbound.send(reply)
        self._accept_outbound_history(reply)
        return {"result": "accepted"}

    def _reserve_outbound_history(
        self, reply: OutboundReply, *, message: InboundMessage
    ) -> None:
        """Reserve an exact outbound body outside accessible history before send.

        Only a gateway-accepted send is promoted into searchable conversation
        history. Failed, pending, and ambiguous attempts remain in the private
        outbox and can never become model context or operator-visible history.
        """

        try:
            self.state.reserve_outbound_conversation_message(
                ConversationMessage(
                    working_session_id=self.current_working_session_id,
                    transport_session_id=reply.session_id,
                    message_id=reply.reply_id,
                    event_id=message.event_id,
                    chat_id=reply.recipient_id,
                    sender_id="jarvis",
                    text=reply.body,
                    occurred_at=self.clock.now(),
                    direction="outbound",
                    request_id=reply.request_id,
                )
            )
        except StateStoreError as exc:
            raise OutboundConnectorError(
                "outbound reply could not be reserved before dispatch",
            ) from exc

    def _accept_outbound_history(self, reply: OutboundReply) -> None:
        """Atomically promote an accepted outbox record into accessible history."""

        try:
            self.state.accept_reserved_outbound_conversation_message(
                transport_session_id=reply.session_id,
                message_id=reply.reply_id,
            )
        except StateStoreError as exc:
            raise OutboundConnectorError(
                "outbound delivery was accepted but history promotion is pending",
                may_have_sent=True,
            ) from exc

    def _send_request_and_finish(
        self,
        reply: OutboundReply,
        token: CancellationToken,
        *,
        outcome: str,
        message: InboundMessage,
    ) -> dict[str, str]:
        """Linearize cancellation against the start of outbound dispatch."""

        with self._dispatch_lock:
            if not cancellation_token_is_current(
                self._current_working_session(), token
            ):
                raise _CancelledBeforeDispatch(
                    "request was cancelled before outbound dispatch"
                )
            self.outbound.send(reply)
            self._accept_outbound_history(reply)
            if not self._finish_session_request(
                token,
                outcome=outcome,
                message=message,
            ):
                raise OutboundConnectorError(
                    "outbound was accepted but session completion is uncertain",
                    may_have_sent=True,
                )
            return {"result": "accepted"}

    def _late_result_result(
        self,
        request: RequestState,
        *,
        message: InboundMessage,
    ) -> ReceiveResult:
        self._best_effort_audit(
            kind="late_result_ignored",
            event_id=message.event_id,
            request_id=request.request_id,
            message_id=message.message_id,
            outcome="ignored",
            actor="control_plane",
            operation_type="request_lifecycle",
            target_category="working_session",
            details={},
        )
        ignored = replace(
            request,
            updated_at=self.clock.now(),
            status="cancelled",
            phase="cancelled",
            outcome="late_result_ignored",
            error_code="cancelled",
        )
        try:
            self.state.update_request(ignored)
        except StateStoreError:
            pass
        return ReceiveResult(
            status_code=202,
            disposition="late_result_ignored",
            request=ignored,
            reason="orchestration result no longer owns the working session",
        )

    def _transition(
        self,
        request: RequestState,
        *,
        audit: bool = True,
        **changes: object,
    ) -> RequestState:
        updated = replace(request, updated_at=self.clock.now(), **changes)
        current = self.state.get_request(request.request_id)
        if current is None:
            self.state.save_request(updated)
        else:
            self.state.update_request(updated)
        if audit:
            try:
                self._append_audit(
                    kind="request_lifecycle",
                    event_id=updated.event_id,
                    request_id=updated.request_id,
                    message_id=updated.message_id,
                    outcome=updated.outcome or updated.status,
                    actor="control_plane",
                    operation_type="request_lifecycle",
                    target_category="control_plane",
                    execution_status=updated.status,
                    details={"phase": updated.phase, "status": updated.status},
                )
            except AuditWriteError:
                if current is not None:
                    self.state.update_request(current)
                raise
        return updated

    def _append_audit(
        self,
        *,
        kind: str,
        event_id: str | None,
        request_id: str | None,
        outcome: str,
        actor: str,
        details: dict[str, str],
        message_id: str | None = None,
        operation_type: str | None = None,
        target_category: str | None = None,
        approval_decision: str | None = None,
        policy_decision: str | None = None,
        execution_status: str | None = None,
    ) -> None:
        self.audit.append(
            self._audit_evidence(
                kind=kind,
                event_id=event_id,
                request_id=request_id,
                outcome=outcome,
                actor=actor,
                details=details,
                message_id=message_id,
                operation_type=operation_type,
                target_category=target_category,
                approval_decision=approval_decision,
                policy_decision=policy_decision,
                execution_status=execution_status,
            )
        )

    def _audit_evidence(
        self,
        *,
        kind: str,
        event_id: str | None,
        request_id: str | None,
        outcome: str,
        actor: str,
        details: dict[str, str],
        message_id: str | None = None,
        operation_type: str | None = None,
        target_category: str | None = None,
        approval_decision: str | None = None,
        policy_decision: str | None = None,
        execution_status: str | None = None,
    ) -> AuditEvidence:
        return AuditEvidence(
            evidence_id=self.ids.new_id("audit"),
            kind=kind,
            occurred_at=self.clock.now(),
            event_id=event_id,
            request_id=request_id,
            outcome=outcome,
            actor=actor,
            details=details,
            message_id=message_id,
            operation_type=operation_type or kind,
            target_category=target_category,
            approval_decision=approval_decision,
            policy_decision=policy_decision,
            execution_status=execution_status,
        )

    def _best_effort_audit(self, **kwargs: object) -> None:
        try:
            self._append_audit(**kwargs)  # ty:ignore[invalid-argument-type]
        except AuditWriteError:
            pass


class SignedMessageReceiver:
    """Real local receiver boundary for signed controlled transport events."""

    def __init__(
        self,
        *,
        config: ControlPlaneConfig,
        state: DurableStateStore,
        audit: AuditBoundary,
        broker: DeterministicCapabilityBroker,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self.config = config
        self.state = state
        self.audit = audit
        self.broker = broker
        self.clock = clock
        self.ids = ids
        self.working_session_id = (
            config.working_session_id or f"working-session-{config.session_id}"
        )

    def receive(self, event: SignedInboundEvent) -> ReceiveResult:
        """Verify, admit, claim, and dispatch one signed event."""

        if len(event.raw_body) > _MAX_RAW_INBOUND_BODY_BYTES:
            return ReceiveResult(
                status_code=413,
                disposition="payload_too_large",
                reason="raw inbound body exceeds the fixed 128 KiB limit",
            )

        if not event.verify(self.config.signing_secret):
            return ReceiveResult(
                status_code=401,
                disposition="unauthenticated",
                reason="signature verification failed",
            )

        try:
            message = event.decode()
        except (TypeError, ValueError):
            self._best_effort_audit(
                kind="inbound_malformed",
                outcome="rejected",
                actor="transport",
                operation_type="inbound_admission",
                target_category="messaging_gateway",
                details={"reason": "malformed_envelope"},
            )
            return ReceiveResult(
                status_code=400,
                disposition="malformed",
                reason="signed event envelope is malformed",
            )

        rejection = self._admission_rejection(message)
        if rejection is not None:
            try:
                admission = self.state.admit_ingress(
                    session_id=message.session_id,
                    message_id=message.message_id,
                    event_id=message.event_id,
                    claimed_at=self.clock.now(),
                    conversation_message=None,
                    audit=self.audit,
                    audit_evidence=self._audit_evidence(
                        kind="inbound_rejected",
                        event_id=message.event_id,
                        outcome="rejected",
                        actor="transport",
                        message_id=message.message_id,
                        operation_type="inbound_admission",
                        target_category="messaging_gateway",
                        details={"reason": rejection},
                    ),
                    terminal_disposition="rejected",
                )
            except StateStoreError:
                return ReceiveResult(
                    status_code=503,
                    disposition="state_unavailable",
                    reason="durable ingress state was unavailable",
                )
            if admission.disposition == "duplicate":
                return ReceiveResult(status_code=204, disposition="duplicate")
            return ReceiveResult(
                status_code=204,
                disposition="rejected",
                reason=rejection,
            )

        try:
            working_session_id = getattr(
                self.broker,
                "current_working_session_id",
                self.working_session_id,
            )
            admission = self.state.admit_ingress(
                session_id=message.session_id,
                message_id=message.message_id,
                event_id=message.event_id,
                claimed_at=self.clock.now(),
                conversation_message=ConversationMessage(
                    working_session_id=working_session_id,
                    transport_session_id=message.session_id,
                    message_id=message.message_id,
                    event_id=message.event_id,
                    chat_id=message.chat_id,
                    sender_id=message.sender_id,
                    text=message.text,
                    occurred_at=self.clock.now(),
                ),
                audit=self.audit,
                audit_evidence=self._audit_evidence(
                    kind="inbound_admitted",
                    event_id=message.event_id,
                    outcome="accepted",
                    actor="configured_operator",
                    message_id=message.message_id,
                    operation_type="inbound_admission",
                    target_category="messaging_gateway",
                    details={"channel": "direct_text", "phase": "admission"},
                ),
                terminal_disposition="admitted",
                audit_blocked_disposition="audit_blocked",
            )
        except StateStoreError:
            return ReceiveResult(
                status_code=503,
                disposition="state_unavailable",
                reason="durable ingress state was unavailable",
            )
        if admission.disposition == "duplicate":
            return ReceiveResult(status_code=204, disposition="duplicate")
        if admission.disposition == "audit_blocked":
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason="required audit evidence was unavailable",
            )
        return self.broker.handle(message)

    def _admission_rejection(self, message: InboundMessage) -> str | None:
        if message.event_type != "message.received":
            return "unsupported_event_type"
        if message.session_id != self.config.session_id:
            return "wrong_session"
        if message.from_me is not False:
            return "self_message"
        if message.chat_type != "direct":
            return "not_direct_message"
        if message.message_type != "text":
            return "unsupported_message_type"
        if not self._identity_is_resolved(
            message.sender_id
        ) or not self._identity_is_resolved(message.chat_id):
            return "unresolved_identity"
        if message.sender_id != self.config.operator_id:
            return "unauthorized_operator"
        if message.chat_id != self.config.operator_id:
            return "unauthorized_chat"
        if not isinstance(message.text, str) or not message.text.strip():
            return "blank_text"
        if len(message.text) > self.config.max_text_length:
            return "text_too_large"
        return None

    @staticmethod
    def _identity_is_resolved(identity: str | None) -> bool:
        return isinstance(identity, str) and bool(identity.strip())

    def _append_audit(
        self,
        *,
        kind: str,
        outcome: str,
        actor: str,
        details: dict[str, str],
        event_id: str | None = None,
        request_id: str | None = None,
        message_id: str | None = None,
        operation_type: str | None = None,
        target_category: str | None = None,
        approval_decision: str | None = None,
        policy_decision: str | None = None,
        execution_status: str | None = None,
    ) -> None:
        self.audit.append(
            self._audit_evidence(
                kind=kind,
                event_id=event_id,
                request_id=request_id,
                outcome=outcome,
                actor=actor,
                details=details,
                message_id=message_id,
                operation_type=operation_type,
                target_category=target_category,
                approval_decision=approval_decision,
                policy_decision=policy_decision,
                execution_status=execution_status,
            )
        )

    def _audit_evidence(
        self,
        *,
        kind: str,
        event_id: str | None = None,
        request_id: str | None = None,
        outcome: str,
        actor: str,
        details: dict[str, str],
        message_id: str | None = None,
        operation_type: str | None = None,
        target_category: str | None = None,
        approval_decision: str | None = None,
        policy_decision: str | None = None,
        execution_status: str | None = None,
    ) -> AuditEvidence:
        return AuditEvidence(
            evidence_id=self.ids.new_id("audit"),
            kind=kind,
            occurred_at=self.clock.now(),
            event_id=event_id,
            request_id=request_id,
            outcome=outcome,
            actor=actor,
            details=details,
            message_id=message_id,
            operation_type=operation_type or kind,
            target_category=target_category,
            approval_decision=approval_decision,
            policy_decision=policy_decision,
            execution_status=execution_status,
        )

    def _best_effort_audit(self, **kwargs: object) -> None:
        try:
            self._append_audit(**kwargs)  # ty:ignore[invalid-argument-type]
        except AuditWriteError:
            pass


@dataclass(frozen=True, slots=True)
class ControlPlane:
    """Facade exposing only the receiver seam to callers."""

    receiver: SignedMessageReceiver

    def receive(self, event: SignedInboundEvent) -> ReceiveResult:
        return self.receiver.receive(event)
