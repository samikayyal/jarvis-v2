"""Receiver and deterministic capability-broker path for ticket01/ticket03."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from threading import RLock

from .control_grammar import ControlTransition, ControlTransitionKind, handle_message
from .models import (
    AuditEvidence,
    ConversationMessage,
    InboundMessage,
    OrchestrationRequest,
    OrchestrationResult,
    OutboundReply,
    ReceiveResult,
    RequestState,
    SignedInboundEvent,
)
from .ports import (
    AuditBoundary,
    AuditWriteError,
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
    InMemoryWorkingSessionStore,
    ModelAvailability,
    RequestResult,
    SessionConfig,
    SessionStoreError,
    TransitionKind,
    WorkingSession,
    WorkingSessionStore,
    apply_request_result,
    cancellation_token_is_current,
    expire_inactive_session,
)
from .traces import DiagnosticTraceRecorder

_MAX_RAW_INBOUND_BODY_BYTES = 128 * 1024


class _CancelledBeforeDispatch(OutboundConnectorError):
    """The request lost ownership before its outbound operation started."""


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
        if self.working_sessions.load() is None:
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
        # The recorder is a write-only capability backed by an isolated writer.
        # Never retain a readable diagnostic store on the broker graph.
        self._trace = trace
        self._dispatch_lock = RLock()
        self._session_lifecycle_lock = RLock()

    def handle(self, message: InboundMessage) -> ReceiveResult:
        """Accept one already-admitted message and drive its named lifecycle stages."""

        session = self._reconcile_inactivity()
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

        return self._complete_outbound_reply(
            message=message,
            request=request,
            cancellation_token=cancellation_token,
            result=result,
        )

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

        orchestration_request = OrchestrationRequest(state=request, text=message.text)
        try:
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
            if result.request_id != request.request_id:
                raise OrchestrationAdapterError(
                    "orchestration result correlation mismatch"
                )
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
        except (
            DiagnosticTraceError,
            OrchestrationAdapterError,
            AuditWriteError,
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
            body=f"{result.reply_text} (request_id={request.request_id})",
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

        if transition.reply is None:
            return ReceiveResult(status_code=202, disposition=transition.kind.value)
        return self._dispatch_control_reply(message, transition)

    def _dispatch_control_reply(
        self,
        message: InboundMessage,
        transition: ControlTransition,
    ) -> ReceiveResult:
        control_id = self.ids.new_id("control")
        reply = OutboundReply(
            reply_id=self.ids.new_id("reply"),
            request_id=control_id,
            session_id=self.config.session_id,
            recipient_id=message.chat_id,
            body=f"{transition.reply or 'Control completed.'} (request_id={control_id})",
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
            self._trace.execute(
                request_id=control_id,
                operation_id=f"{control_id}:connector:outbound",
                operation_type="connector",
                input_payload=reply,
                arguments={"operation": "send", "channel": "controlled_outbound"},
                telemetry={"phase": "control_reply"},
                operation=lambda: self._send_and_confirm(reply),
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
            disposition=transition.kind.value,
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

    def _send_and_confirm(self, reply: OutboundReply) -> dict[str, str]:
        self.outbound.send(reply)
        return {"result": "accepted"}

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
