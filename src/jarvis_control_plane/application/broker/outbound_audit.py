# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker outbound audit workflow."""

from __future__ import annotations

from .support import *


class _BrokerOutboundAuditMixin:
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
        self,
        reply: OutboundReply,
        *,
        message: InboundMessage,
        on_attempt_started: Callable[[], None] | None = None,
    ) -> dict[str, str]:
        self._mark_outbound_attempted(reply, on_started=on_attempt_started)
        delivery = self.outbound.send(reply)
        outbound_id = self._accepted_outbound_id(delivery)
        self._accept_outbound_history(reply, outbound_id=outbound_id)
        return {"outbound_id": outbound_id, "result": "accepted"}

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

    def _accept_outbound_history(
        self, reply: OutboundReply, *, outbound_id: str | None
    ) -> None:
        """Atomically promote an accepted outbox record into accessible history."""

        try:
            self.state.terminalize_outbound_conversation_attempt(
                transport_session_id=reply.session_id,
                message_id=reply.reply_id,
                status=OutboundAttemptStatus.CONFIRMED,
                terminal_at=self.clock.now(),
                outbound_id=outbound_id,
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
        on_attempt_started: Callable[[], None] | None = None,
    ) -> dict[str, str]:
        """Linearize cancellation against the start of outbound dispatch."""

        with self._dispatch_lock:
            if not cancellation_token_is_current(
                self._current_working_session(), token
            ):
                raise _CancelledBeforeDispatch(
                    "request was cancelled before outbound dispatch"
                )
            self._mark_outbound_attempted(reply, on_started=on_attempt_started)
            delivery = self.outbound.send(reply)
            outbound_id = self._accepted_outbound_id(delivery)
            self._accept_outbound_history(reply, outbound_id=outbound_id)
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

    @staticmethod
    def _accepted_outbound_id(delivery: OutboundDelivery) -> str:
        if not isinstance(delivery, OutboundDelivery):
            raise OutboundConnectorError(
                "outbound gateway returned an invalid delivery",
                may_have_sent=True,
            )
        if delivery.accepted is not True:
            raise OutboundConnectorError(
                "outbound gateway outcome was unknown", may_have_sent=True
            )
        outbound_id = delivery.outbound_id
        if not isinstance(outbound_id, str) or not outbound_id.strip():
            raise OutboundConnectorError(
                "outbound gateway identifier was invalid", may_have_sent=True
            )
        return outbound_id

    def _try_terminalize_outbound_attempt(
        self,
        reply: OutboundReply,
        *,
        status: OutboundAttemptStatus,
        outbound_id: str | None = None,
    ) -> StateStoreError | None:
        try:
            self.state.terminalize_outbound_conversation_attempt(
                transport_session_id=reply.session_id,
                message_id=reply.reply_id,
                status=status,
                terminal_at=self.clock.now(),
                outbound_id=outbound_id,
            )
        except StateStoreError as exc:
            return exc
        return None

    def _mark_outbound_attempted(
        self,
        reply: OutboundReply,
        *,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        """Persist the ambiguity boundary immediately before connector entry."""

        try:
            self.state.mark_outbound_conversation_attempted(
                transport_session_id=reply.session_id,
                message_id=reply.reply_id,
                attempted_at=self.clock.now(),
            )
        except StateStoreError as exc:
            raise OutboundConnectorError(
                "outbound attempt could not be persisted before dispatch"
            ) from exc
        if on_started is not None:
            on_started()

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
