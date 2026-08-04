"""Receiver and deterministic capability-broker path for ticket01."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import (
    AuditEvidence,
    ConversationMessage,
    InboundMessage,
    OrchestrationRequest,
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
    OrchestrationAdapter,
    OrchestrationAdapterError,
    OutboundConnector,
    OutboundConnectorError,
    StateStoreError,
    TraceCapacityError,
    TraceWriteError,
    require_non_empty,
)
from .traces import DiagnosticTraceRecorder


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    """Trust configuration for one dedicated messaging session."""

    operator_id: str
    session_id: str
    signing_secret: bytes
    max_text_length: int = 4096

    def __post_init__(self) -> None:
        require_non_empty(self.operator_id, "operator_id")
        require_non_empty(self.session_id, "session_id")
        if not isinstance(self.signing_secret, bytes) or not self.signing_secret:
            raise ValueError("signing_secret must be non-empty bytes")
        if not isinstance(self.max_text_length, int) or self.max_text_length <= 0:
            raise ValueError("max_text_length must be a positive integer")


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
    ) -> None:
        self.config = config
        self.state = state
        self.audit = audit
        self.orchestration = orchestration
        self.outbound = outbound
        self.clock = clock
        self.ids = ids
        self.trace = trace

    def handle(self, message: InboundMessage) -> ReceiveResult:
        """Accept one already-admitted message and drive the typed path."""

        request = RequestState(
            request_id=self.ids.new_id("request"),
            event_id=message.event_id,
            message_id=message.message_id,
            operator_id=self.config.operator_id,
            session_id=self.config.session_id,
            chat_id=message.chat_id,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
            status="accepted",
            phase="orchestration",
        )
        try:
            self.state.save_request(request)
            self._append_audit(
                kind="request_accepted",
                event_id=message.event_id,
                request_id=request.request_id,
                outcome="accepted",
                actor="configured_operator",
                details={"phase": "orchestration"},
            )
        except (StateStoreError, AuditWriteError):
            try:
                blocked = self._transition(
                    request,
                    status="blocked",
                    phase="audit_gate",
                    outcome="audit_unavailable",
                    error_code="audit_unavailable",
                )
            except StateStoreError as transition_error:
                return ReceiveResult(
                    status_code=202,
                    disposition="failed",
                    request=request,
                    reason=(
                        "required audit evidence was unavailable and the blocked state "
                        "could not be persisted: "
                        f"{transition_error}"
                    ),
                )
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                request=blocked,
                reason="required audit evidence was unavailable",
            )

        orchestration_request = OrchestrationRequest(state=request, text=message.text)
        try:
            result = self.trace.execute(
                request_id=request.request_id,
                operation_id=f"{request.request_id}:model",
                operation_type="model",
                input_payload=orchestration_request,
                arguments={
                    "adapter": type(self.orchestration).__name__,
                    "operation": "run",
                },
                telemetry={"phase": "orchestration"},
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
                outcome=result.outcome,
                actor="controlled_orchestration",
                details={"adapter": result.adapter},
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
            except StateStoreError as transition_error:
                self._best_effort_audit(
                    kind="orchestration_result",
                    event_id=message.event_id,
                    request_id=request.request_id,
                    outcome="failed",
                    actor="controlled_orchestration",
                    details={"result": "failed", "state": "unavailable"},
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
                outcome="failed",
                actor="controlled_orchestration",
                details={
                    "result": "failed",
                    "reason": "trace_unavailable" if trace_failed else "orchestration",
                },
            )
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                request=failed,
                reason=str(exc) or "orchestration failed",
            )

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
                outcome=result.outcome,
                error_code=None,
                reply_id=reply.reply_id,
            )
        except StateStoreError as exc:
            self._best_effort_audit(
                kind="outbound_completion",
                event_id=message.event_id,
                request_id=request.request_id,
                outcome="not_sent",
                actor="controlled_outbound",
                details={"result": "not_sent", "state": "unavailable"},
            )
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                request=request,
                reason=f"could not persist replying state; outbound was not sent: {exc}",
            )

        try:
            self.trace.execute(
                request_id=request.request_id,
                operation_id=f"{request.request_id}:connector:outbound",
                operation_type="connector",
                input_payload=reply,
                arguments={"operation": "send", "channel": "controlled_outbound"},
                telemetry={"phase": "outbound"},
                operation=lambda: self._send_and_confirm(reply),
                result_limit_bytes=4_096,
                error_limit_bytes=8_192,
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
            trace_failed = isinstance(exc, (TraceCapacityError, TraceWriteError))
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
            try:
                failed = self._transition(
                    replying,
                    status=("unknown" if may_have_sent else "failed"),
                    phase="outbound",
                    outcome=outcome,
                    error_code=error_code,
                )
            except StateStoreError as transition_error:
                self._best_effort_audit(
                    kind="outbound_completion",
                    event_id=message.event_id,
                    request_id=request.request_id,
                    outcome=outcome,
                    actor="controlled_outbound",
                    details={"result": outcome, "state": "unavailable"},
                )
                return ReceiveResult(
                    status_code=202,
                    disposition="unknown" if may_have_sent else "failed",
                    request=replying,
                    reason=(
                        f"{str(exc) or 'outbound connector failed'}; the {outcome} "
                        "state could not be persisted: "
                        f"{transition_error}"
                    ),
                )
            self._best_effort_audit(
                kind="outbound_completion",
                event_id=message.event_id,
                request_id=request.request_id,
                outcome=outcome,
                actor="controlled_outbound",
                details={
                    "result": outcome,
                    "reason": "trace_unavailable" if trace_failed else "outbound",
                },
            )
            return ReceiveResult(
                status_code=202,
                disposition="unknown" if may_have_sent else "failed",
                request=failed,
                reason=str(exc) or "outbound connector failed",
            )

        try:
            completed = self._transition(
                replying,
                status="completed",
                phase="completed",
                outcome="reply_sent",
                error_code=None,
                reply_id=reply.reply_id,
            )
        except StateStoreError as exc:
            self._best_effort_audit(
                kind="outbound_completion",
                event_id=message.event_id,
                request_id=request.request_id,
                outcome="reply_sent",
                actor="controlled_outbound",
                details={"result": "reply_sent", "state": "unavailable"},
            )
            return ReceiveResult(
                status_code=202,
                disposition="unknown",
                request=replying,
                reply=reply,
                reason=(
                    "outbound reply was accepted, but durable completion state could "
                    f"not be persisted: {exc}"
                ),
            )
        return ReceiveResult(
            status_code=202,
            disposition="completed",
            request=completed,
            reply=reply,
        )

    def _send_and_confirm(self, reply: OutboundReply) -> dict[str, str]:
        self.outbound.send(reply)
        return {"result": "accepted"}

    def _transition(self, request: RequestState, **changes: object) -> RequestState:
        updated = replace(request, updated_at=self.clock.now(), **changes)
        current = self.state.get_request(request.request_id)
        if current is None:
            self.state.save_request(updated)
        else:
            self.state.update_request(updated)
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
    ) -> None:
        self.audit.append(
            AuditEvidence(
                evidence_id=self.ids.new_id("audit"),
                kind=kind,
                occurred_at=self.clock.now(),
                event_id=event_id,
                request_id=request_id,
                outcome=outcome,
                actor=actor,
                details=details,
            )
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

    def receive(self, event: SignedInboundEvent) -> ReceiveResult:
        """Verify, admit, claim, and dispatch one signed event."""

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
                details={"reason": "malformed_envelope"},
            )
            return ReceiveResult(
                status_code=400,
                disposition="malformed",
                reason="signed event envelope is malformed",
            )

        rejection = self._admission_rejection(message)
        if rejection is not None:
            self._best_effort_audit(
                kind="inbound_rejected",
                event_id=message.event_id,
                outcome="rejected",
                actor="transport",
                details={"reason": rejection},
            )
            return ReceiveResult(
                status_code=204,
                disposition="rejected",
                reason=rejection,
            )

        try:
            claimed = self.state.claim_ingress(
                session_id=message.session_id,
                message_id=message.message_id,
                event_id=message.event_id,
                claimed_at=self.clock.now(),
                conversation_message=ConversationMessage(
                    session_id=message.session_id,
                    message_id=message.message_id,
                    event_id=message.event_id,
                    chat_id=message.chat_id,
                    sender_id=message.sender_id,
                    text=message.text,
                    occurred_at=self.clock.now(),
                ),
            )
        except StateStoreError:
            return ReceiveResult(
                status_code=503,
                disposition="state_unavailable",
                reason="durable ingress state was unavailable",
            )
        if not claimed:
            return ReceiveResult(status_code=204, disposition="duplicate")

        try:
            self._append_audit(
                kind="inbound_admitted",
                event_id=message.event_id,
                outcome="accepted",
                actor="configured_operator",
                details={"channel": "direct_text", "phase": "admission"},
            )
        except AuditWriteError:
            try:
                self.state.update_ingress_disposition(
                    session_id=message.session_id,
                    message_id=message.message_id,
                    disposition="audit_blocked",
                )
            except StateStoreError as exc:
                return ReceiveResult(
                    status_code=503,
                    disposition="state_unavailable",
                    reason=(
                        "audit evidence was unavailable and the blocked ingress "
                        f"disposition could not be persisted: {exc}"
                    ),
                )
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason="required audit evidence was unavailable",
            )

        try:
            self.state.update_ingress_disposition(
                session_id=message.session_id,
                message_id=message.message_id,
                disposition="admitted",
            )
        except StateStoreError as exc:
            self._best_effort_audit(
                kind="inbound_admission_finalization_failed",
                event_id=message.event_id,
                outcome="state_unavailable",
                actor="transport",
                details={
                    "phase": "admission_finalization",
                    "disposition": "pending_audit",
                },
            )
            return ReceiveResult(
                status_code=503,
                disposition="state_unavailable",
                reason=(
                    "audit evidence was recorded but the admitted ingress "
                    f"disposition could not be persisted: {exc}"
                ),
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
    ) -> None:
        self.audit.append(
            AuditEvidence(
                evidence_id=self.ids.new_id("audit"),
                kind=kind,
                occurred_at=self.clock.now(),
                event_id=event_id,
                request_id=request_id,
                outcome=outcome,
                actor=actor,
                details=details,
            )
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
