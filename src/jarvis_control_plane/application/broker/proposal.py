# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker proposal workflow."""

from __future__ import annotations

from .support import *


class _BrokerProposalMixin:
    def freeze_action(self, proposal: FrozenActionProposal) -> PendingActionState:
        """Persist one proposal whose exact payload can later be dispatched once."""

        for _ in range(3):
            session = self._current_working_session()
            request = session.active_request
            if request is None or request.request_id != proposal.request_id:
                raise InvariantViolation("proposal does not belong to the live request")
            policy_disposition: str | None = None
            if proposal.kind == "terminal":
                policy = authorize_terminal_proposal(
                    proposal, permissions=session.permissions
                )
                if policy.disposition is TerminalDisposition.HARD_PROHIBITED:
                    raise InvariantViolation("terminal action is hard-prohibited")
                policy_disposition = policy.disposition.value
            action = PendingActionState.from_proposal(
                proposal,
                session_id=session.session_id,
                created_at=self.clock,
                presentation_status=ProposalPresentationStatus.PRESENTING,
                policy_disposition=policy_disposition,
            )
            transition = install_pending_action(session, action, now=self.clock)
            try:
                self._commit_session_with_audit(
                    session,
                    transition.state,
                    kind="pending_action",
                    event_id=None,
                    request_id=proposal.request_id,
                    outcome="pending",
                    actor="control_plane",
                    operation_type="approval_gated_action",
                    target_category="pending_action",
                    details={"action": action.action_id, "state": "frozen"},
                )
            except SessionStoreError:
                continue
            return action
        raise SessionStoreError("pending action could not be frozen atomically")

    def _bind_action_proposal(
        self, proposal: FrozenActionProposal
    ) -> FrozenActionProposal:
        """Let the typed action surface add its current immutable binding."""

        bound = self.action_lifecycle.bind_proposal(proposal)
        if not isinstance(bound, FrozenActionProposal):
            raise InvariantViolation("action dispatcher returned an invalid proposal")
        return bound

    def _present_action(
        self, action: PendingActionState, message: InboundMessage
    ) -> None:
        """Send an all-or-nothing, durable proposal-envelope presentation."""

        fragments = tuple(
            action.preview[index : index + _PROPOSAL_FRAGMENT_PAYLOAD_CHARS]
            for index in range(0, len(action.preview), _PROPOSAL_FRAGMENT_PAYLOAD_CHARS)
        )
        if not fragments:
            raise InvariantViolation("frozen action preview must be non-blank")
        total = len(fragments)
        try:
            for number, fragment in enumerate(fragments, start=1):
                reply = OutboundReply(
                    reply_id=self.ids.new_id("proposal-fragment"),
                    request_id=action.request_id,
                    session_id=self.config.session_id,
                    recipient_id=message.chat_id,
                    quoted_message_id=message.message_id,
                    body=(
                        f"Proposal {action.action_id} digest {action.digest} "
                        f"part {number}/{total} request_id={action.request_id}\n{fragment}"
                    ),
                )
                if len(reply.body) > _MAX_OUTBOUND_MESSAGE_CHARS:
                    raise InvariantViolation(
                        "proposal envelope exceeded outbound bound"
                    )
                outbound_id = self._send_presented_reply(reply, message=message)
                self._record_proposal_fragment(
                    action.action_id, number, total, outbound_id
                )

            prompt = OutboundReply(
                reply_id=self.ids.new_id("proposal-prompt"),
                request_id=action.request_id,
                session_id=self.config.session_id,
                recipient_id=message.chat_id,
                quoted_message_id=message.message_id,
                body=(
                    f"Proposal {action.action_id} digest {action.digest} "
                    "All proposal fragments were presented. "
                    f"{self._proposal_choices(action)} "
                    f"request_id={action.request_id}"
                ),
            )
            if len(prompt.body) > _MAX_OUTBOUND_MESSAGE_CHARS:
                raise InvariantViolation("proposal prompt exceeded outbound bound")
            self._send_presented_reply(prompt, message=message)
            self._mark_proposal_presented(action.action_id)
        except (
            DiagnosticTraceError,
            InvariantViolation,
            OutboundConnectorError,
            SessionStoreError,
            ValueError,
        ):
            self._invalidate_presenting_action(action.action_id)
            raise

    def _send_presented_reply(
        self, reply: OutboundReply, *, message: InboundMessage
    ) -> str:
        """Use the normal audit and trace admission boundary for one envelope send."""

        preflight = getattr(self.outbound, "preflight", None)
        if not callable(preflight):
            raise OutboundConnectorError(
                "outbound connector does not provide audit-safe preflight"
            )
        preflight(reply)
        self.audit.append_batch(
            (
                self._audit_evidence(
                    kind="outbound_attempt",
                    event_id=message.event_id,
                    request_id=reply.request_id,
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
                    request_id=reply.request_id,
                    message_id=message.message_id,
                    outcome="pending",
                    actor="controlled_outbound",
                    operation_type="outbound_message",
                    target_category="operator_conversation",
                    execution_status="pending",
                    details={"channel": "controlled_outbound", "result": "pending"},
                ),
                self._audit_evidence(
                    kind="outbound_completion",
                    event_id=message.event_id,
                    request_id=reply.request_id,
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
        outbound_id: str | None = None
        outbound_attempt_started = False

        def mark_outbound_attempt_started() -> None:
            nonlocal outbound_attempt_started
            outbound_attempt_started = True

        def send() -> dict[str, str]:
            nonlocal outbound_id
            self._mark_outbound_attempted(
                reply, on_started=mark_outbound_attempt_started
            )
            delivery = self.outbound.send(reply)
            outbound_id = self._accepted_outbound_id(delivery)
            self._accept_outbound_history(reply, outbound_id=outbound_id)
            return {"outbound_id": outbound_id, "result": "accepted"}

        try:
            self._trace.execute(
                request_id=reply.request_id,
                operation_id=f"{reply.request_id}:connector:{reply.reply_id}",
                operation_type="connector",
                input_payload=reply,
                arguments={"operation": "send", "channel": "controlled_outbound"},
                telemetry={"phase": "proposal_presentation"},
                operation=send,
                result_limit_bytes=4_096,
                error_limit_bytes=8_192,
            )
        except (DiagnosticTraceError, OutboundConnectorError) as exc:
            may_have_sent = bool(getattr(exc, "may_have_sent", False)) or (
                isinstance(exc, TraceWriteError) and exc.operation_started
            )
            result = "unknown" if may_have_sent else "failed"
            self._try_terminalize_outbound_attempt(
                reply,
                status=(
                    OutboundAttemptStatus.UNKNOWN
                    if outbound_attempt_started
                    else OutboundAttemptStatus.NOT_STARTED
                ),
                outbound_id=outbound_id,
            )
            self._best_effort_audit(
                kind="outbound_result",
                event_id=message.event_id,
                request_id=reply.request_id,
                message_id=message.message_id,
                outcome=result,
                actor="controlled_outbound",
                operation_type="outbound_message",
                target_category="operator_conversation",
                execution_status=result,
                details={"channel": "controlled_outbound", "result": result},
            )
            raise
        self._append_audit(
            kind="outbound_result",
            event_id=message.event_id,
            request_id=reply.request_id,
            message_id=message.message_id,
            outcome="accepted",
            actor="controlled_outbound",
            operation_type="outbound_message",
            target_category="operator_conversation",
            execution_status="accepted",
            details={"channel": "controlled_outbound", "result": "accepted"},
        )
        if outbound_id is None:
            raise InvariantViolation("accepted proposal delivery did not return an ID")
        return outbound_id

    def _record_proposal_fragment(
        self, action_id: str, number: int, total: int, outbound_id: str
    ) -> None:
        current = self._current_working_session()
        transition = record_proposal_fragment(
            current,
            action_id=action_id,
            number=number,
            total=total,
            outbound_id=outbound_id,
            now=self.clock,
        )
        self.working_sessions.compare_and_set(current, transition.state)

    def _mark_proposal_presented(self, action_id: str) -> None:
        current = self._current_working_session()
        transition = mark_proposal_presented(
            current, action_id=action_id, now=self.clock
        )
        self.working_sessions.compare_and_set(current, transition.state)

    def _invalidate_presenting_action(self, action_id: str) -> None:
        """An uncertain fragment result is terminal: never retry or keep payload."""

        for _ in range(3):
            current = self._current_working_session()
            action = current.pending_action
            if action is None or action.action_id != action_id:
                return
            transition = cancel_active_request(
                current, now=self.clock, reason="proposal_presentation_failed"
            )
            try:
                self.working_sessions.compare_and_set(current, transition.state)
                return
            except SessionStoreError:
                continue

    def _consume_pending_approval(
        self,
        message: InboundMessage,
        choice: object,
    ) -> ReceiveResult:
        """Consume approval and wait for dispatch outside the broker lock."""

        # Both human approval and deterministic auto-authorization use this one
        # lifecycle. The lock covers the durable approval boundary, while the
        # external dispatcher preparation runs unlocked so a stalled transport
        # cannot block /cancel or /new.
        with self._dispatch_lock:
            current = self._current_working_session()
            if current.pending_action is None:
                return ReceiveResult(
                    status_code=202,
                    disposition="pending_unavailable",
                )
            if current.pending_action.is_expired(self.clock):
                return self._expire_pending_action(message, current)
            pending_kind = current.pending_action.kind
            approved_or_result = self._approve_pending_action(message, current, choice)
        if isinstance(approved_or_result, ReceiveResult):
            return self._maybe_dispatch_action_ack(
                message, pending_kind, approved_or_result
            )
        prepared_or_result = self._prepare_approved_dispatch(
            message=message,
            action=approved_or_result.action,
            terminal=approved_or_result.terminal,
            permission_id=approved_or_result.permission_id,
        )
        if isinstance(prepared_or_result, ReceiveResult):
            return self._maybe_dispatch_action_ack(
                message, pending_kind, prepared_or_result
            )
        if not self._dispatch_is_still_attempted(prepared_or_result.action.action_id):
            self._release_prepared_dispatch(
                prepared_or_result.action.action_id,
                handle=prepared_or_result.handle,
            )
            self._finalize_dispatch(prepared_or_result.action.action_id)
            return self._late_action_result(
                message,
                prepared_or_result,
                reason="worker registration completed after cancellation",
            )
        result = self._run_prepared_action(message, prepared_or_result)
        return self._maybe_dispatch_action_ack(
            message, prepared_or_result.action.kind, result
        )
