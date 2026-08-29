# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker outbound complete workflow."""

from __future__ import annotations

from .support import *


class _BrokerOutboundCompleteMixin:
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
            quoted_message_id=message.message_id,
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
        outbound_reserved = False
        outbound_attempt_started = False

        def mark_outbound_attempt_started() -> None:
            nonlocal outbound_attempt_started
            outbound_attempt_started = True

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
            outbound_reserved = True
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
                    on_attempt_started=mark_outbound_attempt_started,
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
            terminal_outcome = (
                "read_unavailable" if result.outcome == "unavailable" else "reply_sent"
            )
            completed = self._transition(
                replying,
                status="completed",
                phase="completed",
                outcome=terminal_outcome,
                error_code=None,
                reply_id=reply.reply_id,
                audit=False,
            )
            self._best_effort_audit(
                kind="request_lifecycle",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome=terminal_outcome,
                actor="control_plane",
                operation_type="request_lifecycle",
                target_category="control_plane",
                execution_status="completed",
                details={"phase": "completed", "status": "completed"},
            )
        except _CancelledBeforeDispatch:
            if outbound_reserved:
                self._try_terminalize_outbound_attempt(
                    reply, status=OutboundAttemptStatus.NOT_STARTED
                )
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
            terminalization_error = None
            if outbound_reserved:
                terminalization_error = self._try_terminalize_outbound_attempt(
                    reply,
                    status=(
                        OutboundAttemptStatus.UNKNOWN
                        if outbound_attempt_started
                        else OutboundAttemptStatus.NOT_STARTED
                    ),
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
                if terminalization_error is not None:
                    reason = (
                        f"{reason}; outbound terminal state was not persisted: "
                        f"{terminalization_error}"
                    )
                if may_have_sent:
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
            reason = str(exc) or "outbound connector failed"
            if terminalization_error is not None:
                reason = (
                    f"{reason}; outbound terminal state was not persisted: "
                    f"{terminalization_error}"
                )
            return ReceiveResult(
                status_code=202,
                disposition="unknown" if may_have_sent else "failed",
                request=failed,
                reply=reply if may_have_sent else None,
                reason=reason,
            )

        return ReceiveResult(
            status_code=202,
            disposition=(
                "unavailable" if result.outcome == "unavailable" else "completed"
            ),
            request=completed,
            reply=reply,
        )
