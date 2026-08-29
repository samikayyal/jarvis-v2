# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker dispatch run workflow."""

from __future__ import annotations

from .support import *


class _BrokerDispatchRunMixin:
    def _run_prepared_action(
        self, message: InboundMessage, prepared: _PreparedActionDispatch
    ) -> ReceiveResult:
        """Trace and await a prepared action after its cancellation barrier."""

        execution_result: object | None = None
        try:
            if prepared.action.kind in {"gmail_send", "gmail_reply"}:
                # Gmail owns the complete credential-bearing trace boundary.
                # Re-wrapping it here would reserve a second trace and would
                # change its definite pre-provider failure classification.
                execution_result = prepared.handle.run()
            else:
                execution_result = self._trace.execute(
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
            audit_error: AuditWriteError | None = None
            if prepared.action.kind == "conversation_history_delete":
                try:
                    self._append_conversation_deletion_result(
                        message,
                        prepared.action,
                        terminal_status,
                    )
                except AuditWriteError as audit_exc:
                    audit_error = audit_exc
                    terminal_status = DispatchStatus.UNKNOWN
            if not self._finish_frozen_action(
                prepared.action.action_id, terminal_status
            ):
                return self._late_action_result(message, prepared, reason=str(exc))
            if audit_error is not None:
                return ReceiveResult(
                    status_code=202,
                    disposition="action_dispatch_unknown",
                    reason=(
                        "conversation deletion finished with an uncertain audit "
                        f"outcome: {audit_error}"
                    ),
                )
            if prepared.action.kind == "durable_memory":
                self._best_effort_audit(
                    kind="durable_memory_dispatch",
                    event_id=message.event_id,
                    request_id=prepared.action.request_id,
                    message_id=message.message_id,
                    outcome=terminal_status.value,
                    actor="control_plane",
                    operation_type="durable_memory_mutation",
                    target_category="durable_assistant_memory",
                    execution_status=terminal_status.value,
                    details={
                        "action": prepared.action.action_id,
                        "operation": _memory_action_operation(prepared.action.payload),
                    },
                )
            return ReceiveResult(
                status_code=202,
                disposition=_dispatch_disposition(terminal_status),
                reason=str(exc),
            )
        terminal_status = DispatchStatus.COMPLETED
        audit_error = None
        if prepared.action.kind == "conversation_history_delete":
            try:
                self._append_conversation_deletion_result(
                    message,
                    prepared.action,
                    terminal_status,
                )
            except AuditWriteError as audit_exc:
                audit_error = audit_exc
                terminal_status = DispatchStatus.UNKNOWN
        if not self._finish_frozen_action(prepared.action.action_id, terminal_status):
            return self._late_action_result(
                message,
                prepared,
                reason="action completed but terminal state was already closed",
            )
        if audit_error is not None:
            return ReceiveResult(
                status_code=202,
                disposition="action_dispatch_unknown",
                reason=(
                    "conversation deletion may have completed but its terminal "
                    f"audit outcome was unavailable: {audit_error}"
                ),
            )
        if prepared.action.kind == "durable_memory":
            self._best_effort_audit(
                kind="durable_memory_dispatch",
                event_id=message.event_id,
                request_id=prepared.action.request_id,
                message_id=message.message_id,
                outcome="completed",
                actor="control_plane",
                operation_type="durable_memory_mutation",
                target_category="durable_assistant_memory",
                execution_status="completed",
                details={
                    "action": prepared.action.action_id,
                    "operation": _memory_action_operation(prepared.action.payload),
                },
            )
        reason = None
        if prepared.action.kind == "terminal":
            if not isinstance(execution_result, WorkerExecutionResult):
                return ReceiveResult(
                    status_code=202,
                    disposition="action_dispatch_unknown",
                    reason="terminal action completed without a typed bounded result",
                )
            terminal = terminal_action_from_proposal(prepared.action)
            reason = _render_terminal_result(terminal, execution_result)
        return ReceiveResult(
            status_code=202,
            disposition="action_dispatched",
            reason=reason,
        )

    def _append_conversation_deletion_result(
        self,
        message: InboundMessage,
        action: FrozenActionProposal,
        status: DispatchStatus,
    ) -> None:
        """Record the redacted terminal result before closing the action."""

        if status is DispatchStatus.COMPLETED:
            outcome = "completed"
            execution_status = "completed"
        elif status is DispatchStatus.UNKNOWN:
            outcome = "unknown"
            execution_status = "unknown"
        elif status is DispatchStatus.NOT_STARTED:
            outcome = "failed"
            execution_status = "not_started"
        else:
            outcome = "failed"
            execution_status = "failed"
        self._append_audit(
            kind="conversation_history_deletion_result",
            event_id=message.event_id,
            request_id=action.request_id,
            message_id=message.message_id,
            outcome=outcome,
            actor="control_plane",
            operation_type="conversation_history_delete",
            target_category="operator_conversation",
            approval_decision="approved",
            execution_status=execution_status,
            details={"action": action.action_id, "result": outcome},
        )

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

        dispatcher = self._dispatcher_for_action_id(action_id)
        try:
            if handle is not None and callable(getattr(handle, "cancel", None)):
                handle.cancel()  # type: ignore[attr-defined]
            else:
                dispatcher.cancel(action_id=action_id)
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

        dispatcher = self._dispatcher_for_action_id(action_id)
        if not isinstance(dispatcher, ActionFinalizer):
            return
        try:
            dispatcher.finalize(action_id=action_id)
        except Exception:  # noqa: BLE001 - bounded retention is the fallback
            return

    def _dispatcher_for_action_kind(self, kind: str) -> ActionDispatcher:
        return (
            self.memory_action_dispatcher
            if kind == "durable_memory"
            else self.action_dispatcher
        )

    def _dispatcher_for_action_id(self, action_id: str) -> ActionDispatcher:
        try:
            record = next(
                item
                for item in self._current_working_session().action_outbox
                if item.action_id == action_id
            )
        except (SessionStoreError, StopIteration):
            return self.action_dispatcher
        return self._dispatcher_for_action_kind(record.kind)
