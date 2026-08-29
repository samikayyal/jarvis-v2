# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker dispatch prepare workflow."""

from __future__ import annotations

from .support import *


class _BrokerDispatchPrepareMixin:
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
        dispatcher = self._dispatcher_for_action_kind(frozen_or_result.kind)
        try:
            handle = dispatcher.prepare(frozen_or_result)
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
            if action.kind == "durable_memory":
                self._best_effort_audit(
                    kind="durable_memory_dispatch",
                    event_id=message.event_id,
                    request_id=action.request_id,
                    message_id=message.message_id,
                    outcome=terminal_status.value,
                    actor="control_plane",
                    operation_type="durable_memory_mutation",
                    target_category="durable_assistant_memory",
                    execution_status=terminal_status.value,
                    details={
                        "action": action.action_id,
                        "operation": _memory_action_operation(frozen_or_result.payload),
                    },
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
        if action.kind == "durable_memory":
            try:
                self._append_audit(
                    kind="durable_memory_dispatch",
                    event_id=message.event_id,
                    request_id=action.request_id,
                    message_id=message.message_id,
                    outcome="attempted",
                    actor="control_plane",
                    operation_type="durable_memory_mutation",
                    target_category="durable_assistant_memory",
                    approval_decision="approved",
                    execution_status="not_started",
                    details={
                        "action": action.action_id,
                        "operation": _memory_action_operation(action.payload),
                    },
                )
            except AuditWriteError as exc:
                self._close_unattempted_action(action.action_id)
                return ReceiveResult(
                    status_code=202,
                    disposition="audit_blocked",
                    reason=f"durable-memory dispatch was blocked by audit: {exc}",
                )
        try:
            attempted = mark_action_dispatch_attempted(
                dispatching, action_id=action.action_id, now=self.clock
            )
            self.working_sessions.compare_and_set(dispatching, attempted.state)
        except (InvariantViolation, SessionStoreError) as exc:
            if action.kind == "conversation_history_delete":
                frozen_action = FrozenActionProposal(
                    action_id=action.action_id,
                    request_id=action.request_id,
                    kind=action.kind,
                    preview=action.preview or "",
                    payload=action.payload,
                    digest=action.digest,
                )
                terminal_status = DispatchStatus.NOT_STARTED
                audit_error: AuditWriteError | None = None
                try:
                    self._append_conversation_deletion_result(
                        message,
                        frozen_action,
                        terminal_status,
                    )
                except AuditWriteError as audit_exc:
                    audit_error = audit_exc
                    terminal_status = DispatchStatus.UNKNOWN
                if not self._finish_frozen_action(action.action_id, terminal_status):
                    return ReceiveResult(
                        status_code=202,
                        disposition="action_dispatch_unknown",
                        reason=(
                            "conversation deletion dispatch boundary failed and its "
                            f"terminal state could not be persisted: {exc}"
                        ),
                    )
                if audit_error is not None:
                    return ReceiveResult(
                        status_code=202,
                        disposition="action_dispatch_unknown",
                        reason=(
                            "conversation deletion did not start but its terminal "
                            f"audit outcome was unavailable: {audit_error}"
                        ),
                    )
                return ReceiveResult(
                    status_code=202,
                    disposition="action_dispatch_not_started",
                    reason=f"dispatch attempt was not durably recorded: {exc}",
                )
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
