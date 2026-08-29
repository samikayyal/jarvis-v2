# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker memory commands workflow."""

from __future__ import annotations

from .support import *


class _BrokerMemoryCommandsMixin:
    def _handle_memory_command(
        self,
        message: InboundMessage,
        command: MemoryCommand,
    ) -> ReceiveResult:
        """Keep memory reads explicit and route every write through approval."""

        if command.is_valid and command.operation is not MemoryOperation.USE:
            blocked = self._memory_command_blocked(message, command)
            if blocked is not None:
                return blocked
        if not command.is_valid:
            try:
                self._append_audit(
                    kind="durable_memory_invalid",
                    event_id=message.event_id,
                    request_id=None,
                    message_id=message.message_id,
                    outcome="rejected",
                    actor="configured_operator",
                    operation_type="durable_memory",
                    target_category="durable_assistant_memory",
                    details={"operation": MemoryOperation.INVALID.value},
                )
            except AuditWriteError as exc:
                return ReceiveResult(
                    status_code=202,
                    disposition="audit_blocked",
                    reason=f"memory command was blocked by audit: {exc}",
                )
            return self._dispatch_memory_text(
                message,
                command.error or "Invalid durable-memory command.",
                disposition="memory_invalid",
            )
        if command.operation is MemoryOperation.USE:
            return self._handle_memory_use(message, command)
        if command.is_read:
            return self._handle_memory_read(message, command)
        return self._handle_memory_mutation(message, command)

    def _memory_command_blocked(
        self,
        message: InboundMessage,
        command: MemoryCommand,
    ) -> ReceiveResult | None:
        """Apply the working-session gate before any memory read or use."""

        current = self._current_working_session()
        if current.pending_action is not None:
            kind = ControlTransitionKind.PENDING_BLOCKED
            reply = (
                "A pending action blocks this durable-memory command. "
                "Approve or reject it first."
            )
            reason = "a pending action blocks unrelated durable-memory work"
            effect = "request_refused_pending"
        elif current.active_request is not None or any(
            record.is_open for record in current.action_outbox
        ):
            kind = ControlTransitionKind.BUSY_REFUSED
            reply = (
                "Another request is active, so this durable-memory command was "
                "refused. Use /status or /cancel; V1 does not queue work."
            )
            reason = "one active request is already present; no queue transition"
            effect = "request_refused_busy"
        else:
            return None

        return self._apply_session_control(
            message,
            current,
            ControlTransition(
                state=current,
                parsed=parse_control(message.text),
                kind=kind,
                reply=reply,
                effects=(effect,),
                reason=reason,
            ),
        )

    def _handle_memory_use(
        self,
        message: InboundMessage,
        command: MemoryCommand,
    ) -> ReceiveResult:
        """Run one request with exactly one operator-selected memory record."""

        assert command.memory_id is not None
        assert command.content is not None
        try:
            self._append_audit(
                kind="durable_memory_access",
                event_id=message.event_id,
                request_id=None,
                message_id=message.message_id,
                outcome="requested",
                actor="configured_operator",
                operation_type="durable_memory_read",
                target_category="durable_assistant_memory",
                details={
                    "operation": MemoryOperation.USE.value,
                    "target": command.memory_id,
                },
            )
            target = self.state.get_memory(command.memory_id)
        except AuditWriteError as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"durable-memory selection was blocked by audit: {exc}",
            )
        except (StateStoreError, ValueError) as exc:
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                reason=f"durable-memory selection failed: {exc}",
            )
        if target is None or not target.is_active or target.content is None:
            return self._dispatch_memory_text(
                message,
                "No active durable memory has that exact ID.",
                disposition="memory_target_missing",
            )

        try:
            session = self._current_working_session()
        except SessionStoreError as exc:
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                reason=f"durable-memory request could not start: {exc}",
            )
        admission = self._admit_orchestration_request(
            message=message,
            session=session,
            request_text=command.content,
        )
        if isinstance(admission, ReceiveResult):
            return admission
        selection = MemorySelection(memories=(target,), explicit=True)
        result = self._run_orchestration(
            message=message,
            request=admission.request,
            cancellation_token=admission.cancellation_token,
            orchestration_text=command.content,
            memory_selection=selection,
        )
        if isinstance(result, ReceiveResult):
            return result
        return self._complete_orchestration_result(
            message=message,
            request=admission.request,
            cancellation_token=admission.cancellation_token,
            result=result,
        )

    def _handle_memory_read(
        self, message: InboundMessage, command: MemoryCommand
    ) -> ReceiveResult:
        try:
            self._append_audit(
                kind="durable_memory_access",
                event_id=message.event_id,
                request_id=None,
                message_id=message.message_id,
                outcome="requested",
                actor="configured_operator",
                operation_type="durable_memory_read",
                target_category="durable_assistant_memory",
                details={
                    "operation": command.operation.value,
                    "target": command.memory_id or "none",
                },
            )
            if command.operation is MemoryOperation.LIST:
                memories = self.state.list_memories(include_terminal=True, limit=20)
                body = self._render_memory_list(memories)
            elif command.operation is MemoryOperation.SEARCH:
                assert command.content is not None
                memories = self.state.search_memories(
                    text=command.content,
                    include_terminal=True,
                    limit=20,
                )
                body = self._render_memory_list(memories, searched=True)
            else:
                assert command.memory_id is not None
                memory = self.state.get_memory(command.memory_id)
                body = self._render_memory_inspect(memory)
        except AuditWriteError as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"durable-memory read was blocked by audit: {exc}",
            )
        except MemorySearchLimitExceeded:
            return self._dispatch_memory_text(
                message,
                "Durable-memory search reached its bounded scan limit; "
                "narrow the search and try again.",
                disposition="memory_search_limited",
            )
        except (StateStoreError, ValueError) as exc:
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                reason=f"durable-memory read failed: {exc}",
            )
        if command.operation is MemoryOperation.INSPECT:
            return self._dispatch_exact_text_export(
                message,
                body,
                label="Durable-memory inspection",
                disposition="memory_inspect",
            )
        return self._dispatch_memory_text(
            message, body, disposition=f"memory_{command.operation.value}"
        )

    def _handle_memory_mutation(
        self,
        message: InboundMessage,
        command: MemoryCommand,
    ) -> ReceiveResult:
        try:
            self._append_audit(
                kind="durable_memory_mutation",
                event_id=message.event_id,
                request_id=None,
                message_id=message.message_id,
                outcome="requested",
                actor="configured_operator",
                operation_type="durable_memory_mutation",
                target_category="durable_assistant_memory",
                details={"operation": command.operation.value},
            )
        except AuditWriteError as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"durable-memory change was blocked by audit: {exc}",
            )
        target_result = self._memory_mutation_target(message, command)
        if isinstance(target_result, ReceiveResult):
            return target_result
        target = target_result
        admission = self._admit_memory_request(message)
        if isinstance(admission, ReceiveResult):
            return admission
        request = admission.request
        token = admission.cancellation_token
        try:
            proposal = self._memory_action_proposal(
                message=message,
                request=request,
                command=command,
                target=target,
            )
            action = self.freeze_action(proposal)
            self._present_action(action, message)
        except (
            AuditWriteError,
            DiagnosticTraceError,
            InvariantViolation,
            OutboundConnectorError,
            SessionStoreError,
            ValueError,
        ) as exc:
            return self._finish_proposal_failure(
                message=message,
                request=request,
                token=token,
                error=exc,
            )
        return ReceiveResult(
            status_code=202,
            disposition="pending_action",
            request=request,
            reason="explicit durable-memory change is frozen pending operator approval",
        )

    def _memory_mutation_target(
        self,
        message: InboundMessage,
        command: MemoryCommand,
    ) -> DurableMemory | ReceiveResult | None:
        if command.operation is MemoryOperation.REMEMBER:
            return None
        assert command.memory_id is not None
        try:
            target = self.state.get_memory(command.memory_id)
        except (StateStoreError, ValueError) as exc:
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                reason=f"durable-memory target could not be inspected: {exc}",
            )
        if target is None:
            return self._dispatch_memory_text(
                message,
                "No active durable memory has that exact ID.",
                disposition="memory_target_missing",
            )
        if not target.is_active:
            return self._dispatch_memory_text(
                message,
                "That durable memory is already terminal and cannot be changed.",
                disposition="memory_target_terminal",
            )
        return target

    def _admit_memory_request(
        self, message: InboundMessage
    ) -> _RequestAdmission | ReceiveResult:
        try:
            current = self._current_working_session()
            request_id = self.ids.new_id("request")
            accepted = accept_request(
                current,
                now=self.clock,
                request_id=request_id,
                originating_message_id=message.message_id,
                phase="processing",
            )
        except (SessionStoreError, ValueError) as exc:
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                reason=f"durable-memory request could not start: {exc}",
            )
        if accepted.kind is not TransitionKind.REQUEST_ACCEPTED:
            return self._memory_request_blocked(message, current, accepted)
        session_transition = self._memory_request_transition(message, accepted)
        return self._admit_request(
            message=message,
            session=current,
            session_transition=session_transition,
            request_id=request_id,
        )

    def _memory_request_blocked(
        self,
        message: InboundMessage,
        current: WorkingSession,
        accepted: SessionTransition,
    ) -> ReceiveResult:
        blocked_kind = (
            ControlTransitionKind.PENDING_BLOCKED
            if accepted.kind is TransitionKind.PENDING_BLOCKED
            else ControlTransitionKind.BUSY_REFUSED
        )
        blocked_reply = (
            "A durable-memory change cannot start while an approval is pending. "
            "Approve or reject it first."
            if blocked_kind is ControlTransitionKind.PENDING_BLOCKED
            else "A durable-memory change cannot start while another request is active."
        )
        return self._apply_session_control(
            message,
            current,
            ControlTransition(
                state=current,
                parsed=parse_control(message.text),
                kind=blocked_kind,
                reply=blocked_reply,
                effects=accepted.effects,
                reason=accepted.reason,
            ),
        )

    @staticmethod
    def _memory_request_transition(
        message: InboundMessage, accepted: SessionTransition
    ) -> ControlTransition:
        return ControlTransition(
            state=accepted.state,
            parsed=parse_control(message.text),
            kind=ControlTransitionKind.REQUEST_ACCEPTED,
            effects=accepted.effects,
            cancellation_token=accepted.cancellation_token,
            reason=accepted.reason,
        )

    def _memory_action_proposal(
        self,
        *,
        message: InboundMessage,
        request: RequestState,
        command: MemoryCommand,
        target: DurableMemory | None,
    ) -> FrozenActionProposal:
        if command.operation is MemoryOperation.REMEMBER:
            preview, payload = self._remember_memory_proposal(message, command)
        elif command.operation is MemoryOperation.REPLACE:
            preview, payload = self._replace_memory_proposal(message, command, target)
        else:
            assert command.operation is MemoryOperation.FORGET
            preview, payload = self._forget_memory_proposal(message, target)
        return FrozenActionProposal.create(
            action_id=self.ids.new_id("action"),
            request_id=request.request_id,
            kind="durable_memory",
            preview=preview,
            payload=payload,
        )
