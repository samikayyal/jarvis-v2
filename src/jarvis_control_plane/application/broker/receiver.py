# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Signed message admission boundary."""

from __future__ import annotations

from .support import *


class _SignedMessageReceiverBase:
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

        return self._receive(event, dispatch=True)

    def admit(self, event: SignedInboundEvent) -> ReceiveResult:
        """Durably admit a webhook event without running assistant work inline."""

        return self._receive(event, dispatch=False)

    def dispatch_admitted_message(self, message: InboundMessage) -> ReceiveResult:
        """Run one message already claimed by the durable ingress worker."""

        return self.broker.handle(message)

    def reconcile_ingress_restart(self) -> int:
        """Fail closed on nonterminal ingress left by a prior process."""

        return self.state.reconcile_ingress_restart(
            audit=self.audit,
            audit_evidence=self._audit_evidence(
                kind="service_restart",
                outcome="interrupted",
                actor="control_plane",
                operation_type="working_session",
                target_category="working_session",
                execution_status="recorded",
                details={"interrupted_ingress": "nonterminal"},
            ),
        )

    def _receive(self, event: SignedInboundEvent, *, dispatch: bool) -> ReceiveResult:
        """Shared signed admission path for synchronous and HTTP boundaries."""

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

        if self.broker.recovery_degraded:
            return ReceiveResult(
                status_code=503,
                disposition="recovery_degraded",
                reason=self.broker.recovery_degraded_reason,
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
        if dispatch:
            try:
                began = self.state.begin_ingress_dispatch(
                    transport_session_id=message.session_id,
                    message_id=message.message_id,
                )
            except StateStoreError:
                return ReceiveResult(
                    status_code=503,
                    disposition="state_unavailable",
                    reason="durable ingress dispatch state was unavailable",
                )
            if not began:
                return ReceiveResult(
                    status_code=503,
                    disposition="dispatch_unavailable",
                    reason="admitted ingress work could not be claimed",
                )
            try:
                result = self.broker.handle(message)
            except Exception:
                self.state.finish_ingress_dispatch(
                    transport_session_id=message.session_id,
                    message_id=message.message_id,
                    disposition="interrupted",
                )
                raise
            self.state.finish_ingress_dispatch(
                transport_session_id=message.session_id,
                message_id=message.message_id,
                disposition="dispatched",
            )
            return result
        return ReceiveResult(status_code=202, disposition="admitted")

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
