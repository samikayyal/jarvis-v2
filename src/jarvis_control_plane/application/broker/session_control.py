# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker session control workflow."""

from __future__ import annotations

from .support import *


class _BrokerSessionControlMixin:
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
            cancelled_request_id = (
                expected.active_request.request_id
                if expected.active_request is not None
                else None
            )
            cancel_orchestration = getattr(self.orchestration, "cancel", None)
            if cancelled_request_id is not None and callable(cancel_orchestration):
                try:
                    cancel_orchestration(request_id=cancelled_request_id)
                except (
                    OrchestrationAdapterError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    self._best_effort_audit(
                        kind="orchestration_cancellation_failed",
                        event_id=message.event_id,
                        request_id=cancelled_request_id,
                        message_id=message.message_id,
                        outcome="unknown",
                        actor="control_plane",
                        operation_type="orchestration_cancellation",
                        target_category="model",
                        details={"reason": type(exc).__name__},
                    )
                    return ReceiveResult(
                        status_code=202,
                        disposition="cancellation_unknown",
                        reason=(
                            "cancellation was recorded but the active orchestration "
                            "process did not establish quiescence"
                        ),
                    )
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
            dispatcher = self._dispatcher_for_action_kind(kind)
            try:
                result = dispatcher.cancel(action_id=action_id)
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
            quoted_message_id=message.message_id,
            body=body,
        )
        outbound_reserved = False
        outbound_attempt_started = False

        def mark_outbound_attempt_started() -> None:
            nonlocal outbound_attempt_started
            outbound_attempt_started = True

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
            outbound_reserved = True
            self._trace.execute(
                request_id=control_id,
                operation_id=f"{control_id}:connector:outbound",
                operation_type="connector",
                input_payload=reply,
                arguments={"operation": "send", "channel": "controlled_outbound"},
                telemetry={"phase": "control_reply"},
                operation=lambda: self._send_and_confirm(
                    reply,
                    message=message,
                    on_attempt_started=mark_outbound_attempt_started,
                ),
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
            reason = str(exc) or "control reply failed"
            if terminalization_error is not None:
                reason = f"{reason}; outbound terminal state was not persisted: {terminalization_error}"
            return ReceiveResult(
                status_code=202,
                disposition="unknown" if may_have_sent else "failed",
                reply=reply if may_have_sent else None,
                reason=reason,
            )
        return ReceiveResult(
            status_code=202,
            disposition=disposition,
            reply=reply,
        )
