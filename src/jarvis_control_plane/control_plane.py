"""Receiver and deterministic capability-broker path for ticket01/ticket03."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import (
    AuditEvidence,
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
    DurableStateStore,
    IdGenerator,
    OrchestrationAdapter,
    OrchestrationAdapterError,
    OutboundConnector,
    OutboundConnectorError,
    StateStoreError,
    require_non_empty,
)


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
    ) -> None:
        self.config = config
        self.state = state
        self.audit = audit
        self.orchestration = orchestration
        self.outbound = outbound
        self.clock = clock
        self.ids = ids

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
                message_id=message.message_id,
                outcome="accepted",
                actor="configured_operator",
                operation_type="request_lifecycle",
                target_category="control_plane",
                details={"phase": "orchestration"},
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

        orchestration_request = OrchestrationRequest(state=request, text=message.text)
        try:
            result = self.orchestration.run(orchestration_request)
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
                details={"adapter": result.adapter},
            )
        except (OrchestrationAdapterError, AuditWriteError, ValueError) as exc:
            try:
                failed = self._transition(
                    request,
                    status="failed",
                    phase="orchestration",
                    outcome="orchestration_failed",
                    error_code="orchestration_error",
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
                )
            )
            self.outbound.send(reply)
            side_effect_may_have_happened = True
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
            completed = self._transition(
                replying,
                status="completed",
                phase="completed",
                outcome="reply_sent",
                error_code=None,
                reply_id=reply.reply_id,
            )
        except (
            AuditWriteError,
            OutboundConnectorError,
            StateStoreError,
            ValueError,
        ) as exc:
            may_have_sent = side_effect_may_have_happened or (
                isinstance(exc, OutboundConnectorError) and exc.may_have_sent
            )
            outcome = "outbound_unknown" if may_have_sent else "outbound_failed"
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
                    status="unknown" if may_have_sent else "failed",
                    phase="outbound",
                    outcome=outcome,
                    error_code="outbound_unknown"
                    if may_have_sent
                    else "outbound_error",
                    audit=True,
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
        except ValueError:
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
            self._best_effort_audit(
                kind="inbound_rejected",
                event_id=message.event_id,
                outcome="rejected",
                actor="transport",
                message_id=message.message_id,
                operation_type="inbound_admission",
                target_category="messaging_gateway",
                details={"reason": rejection},
            )
            return ReceiveResult(
                status_code=204,
                disposition="rejected",
                reason=rejection,
            )

        try:
            already_claimed = self.state.has_ingress_claim(
                session_id=message.session_id,
                message_id=message.message_id,
            )
        except StateStoreError:
            return ReceiveResult(
                status_code=503,
                disposition="state_unavailable",
                reason="durable ingress state was unavailable",
            )
        if already_claimed:
            return ReceiveResult(status_code=204, disposition="duplicate")

        try:
            self._append_audit(
                kind="inbound_admitted",
                event_id=message.event_id,
                outcome="accepted",
                actor="configured_operator",
                message_id=message.message_id,
                operation_type="inbound_admission",
                target_category="messaging_gateway",
                details={"channel": "direct_text", "phase": "admission"},
            )
        except AuditWriteError:
            try:
                claim_reserved = self.state.claim_ingress(
                    session_id=message.session_id,
                    message_id=message.message_id,
                    event_id=message.event_id,
                    claimed_at=self.clock.now(),
                )
            except StateStoreError:
                return ReceiveResult(
                    status_code=503,
                    disposition="state_unavailable",
                    reason=(
                        "required audit evidence was unavailable and the durable "
                        "replay claim could not be preserved"
                    ),
                )
            if not claim_reserved:
                return ReceiveResult(status_code=204, disposition="duplicate")
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason="required audit evidence was unavailable",
            )

        try:
            claimed = self.state.claim_ingress(
                session_id=message.session_id,
                message_id=message.message_id,
                event_id=message.event_id,
                claimed_at=self.clock.now(),
            )
        except StateStoreError:
            return ReceiveResult(
                status_code=503,
                disposition="state_unavailable",
                reason="durable ingress state was unavailable",
            )
        if not claimed:
            return ReceiveResult(status_code=204, disposition="duplicate")

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
        if message.sender_id != self.config.operator_id:
            return "unauthorized_operator"
        if message.chat_id != self.config.operator_id:
            return "unauthorized_chat"
        if not message.text.strip():
            return "blank_text"
        if len(message.text) > self.config.max_text_length:
            return "text_too_large"
        return None

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
            AuditEvidence(
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
