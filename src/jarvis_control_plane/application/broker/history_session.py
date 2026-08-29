# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker history session workflow."""

from __future__ import annotations

from .support import *


class _BrokerHistorySessionMixin:
    @property
    def current_working_session_id(self) -> str:
        """Expose only the current conversation boundary to ingress admission."""

        return self._reconcile_inactivity().session_id

    @property
    def recovery_degraded(self) -> bool:
        """Whether restart found state requiring manual administrative repair."""

        return self._recovery_degraded

    @property
    def recovery_degraded_reason(self) -> str | None:
        """Return only the bounded administrative recovery reason code."""

        return self._recovery_degraded_reason

    def _current_working_session(self) -> WorkingSession:
        session = self.working_sessions.load()
        if session is None:
            raise SessionStoreError("working session is unavailable")
        return session

    def _reconcile_inactivity(self) -> WorkingSession:
        """Advance an idle working session before it receives another message."""

        with self._session_lifecycle_lock:
            for _ in range(3):
                session = self._current_working_session()
                transition = expire_inactive_session(session, now=self.clock)
                if transition.kind is not TransitionKind.SESSION_EXPIRED:
                    return session
                self._append_audit(
                    kind="working_session_expired",
                    event_id=None,
                    request_id=None,
                    outcome="expired",
                    actor="control_plane",
                    operation_type="working_session_lifecycle",
                    target_category="working_session",
                    details={},
                )
                try:
                    self.working_sessions.compare_and_set(session, transition.state)
                    return transition.state
                except SessionStoreError:
                    continue
        raise SessionStoreError("working-session inactivity reconciliation raced")

    def _model_availability(self) -> ModelAvailability:
        try:
            availability = self.model_availability_provider.current()
        except Exception as exc:
            raise RuntimeError("availability provider check failed") from exc
        if not isinstance(availability, ModelAvailability):
            raise TypeError("model availability provider returned an invalid value")
        return availability

    def _handle_history_deletion_control(
        self,
        message: InboundMessage,
        args: tuple[str, ...],
    ) -> ReceiveResult:
        """Preview one exact history scope, then freeze it behind approval."""

        try:
            self._append_audit(
                kind="conversation_history_deletion_preview",
                event_id=message.event_id,
                request_id=None,
                message_id=message.message_id,
                outcome="requested",
                actor="configured_operator",
                operation_type="conversation_history_delete",
                target_category="operator_conversation",
                details={"scope": args[1]},
            )
        except AuditWriteError as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"conversation deletion preview was blocked by audit: {exc}",
            )

        current = self._current_working_session()
        if current.pending_action is not None:
            return self._dispatch_history_text(
                message,
                "A pending action already owns this working session. Approve, reject, "
                "or cancel it before requesting deletion.",
                disposition="pending_blocked",
            )
        if current.active_request is not None or any(
            record.is_open for record in current.action_outbox
        ):
            return self._dispatch_history_text(
                message,
                "A request is still active. Use /status or /cancel before requesting "
                "conversation deletion.",
                disposition="busy_refused",
            )

        try:
            scope = self._parse_history_deletion_scope(args)
            if (
                scope.scope_type == "conversation"
                and scope.conversation_id == current.session_id
            ):
                return self._dispatch_history_text(
                    message,
                    "The active conversation cannot be deleted while it is open. "
                    "Use /new first, then delete the closed conversation.",
                    disposition="history_delete_current_refused",
                )
            preview = self.state.preview_conversation_deletion(scope)
        except (StateStoreError, TypeError, ValueError) as exc:
            return self._dispatch_history_text(
                message,
                f"Conversation deletion preview failed: {type(exc).__name__}.",
                disposition="history_delete_failed",
            )
        if not preview.messages:
            return self._dispatch_history_text(
                message,
                "No accessible conversation messages matched that deletion scope.",
                disposition="history_delete_empty",
            )

        request_id = self.ids.new_id("request")
        session_transition = accept_request(
            current,
            now=self.clock,
            request_id=request_id,
            originating_message_id=message.message_id,
            phase="processing",
        )
        if session_transition.kind is not TransitionKind.REQUEST_ACCEPTED:
            return self._dispatch_history_text(
                message,
                "Conversation deletion could not start because the working session "
                "changed. Use /status and try again.",
                disposition="history_delete_busy",
            )
        admission = self._admit_request(
            message=message,
            session=current,
            session_transition=ControlTransition(
                state=session_transition.state,
                parsed=parse_control(message.text),
                kind=ControlTransitionKind.REQUEST_ACCEPTED,
                cancellation_token=session_transition.cancellation_token,
            ),
            request_id=request_id,
        )
        if isinstance(admission, ReceiveResult):
            return admission

        proposal = FrozenActionProposal.create(
            action_id=self.ids.new_id("history-delete"),
            request_id=admission.request.request_id,
            kind="conversation_history_delete",
            preview=self._render_history_deletion_preview(preview),
            payload=_deletion_payload(preview),
        )
        try:
            action = self.freeze_action(proposal)
            self._present_action(action, message)
        except (
            AuditWriteError,
            ActionDispatcherError,
            DiagnosticTraceError,
            InvariantViolation,
            OutboundConnectorError,
            SessionStoreError,
            StateStoreError,
            ValueError,
        ) as exc:
            return self._finish_proposal_failure(
                message=message,
                request=admission.request,
                token=admission.cancellation_token,
                error=exc,
            )
        return ReceiveResult(
            status_code=202,
            disposition="pending_action",
            request=admission.request,
            reason="exact conversation deletion proposal is frozen pending approval",
        )

    @staticmethod
    def _parse_history_deletion_scope(
        args: tuple[str, ...],
    ) -> ConversationDeletionScope:
        if len(args) < 3 or args[0] != "delete":
            raise ValueError("history deletion arguments are malformed")
        selector_type = args[1]
        if selector_type == "message" and len(args) == 3:
            return ConversationDeletionScope.message(args[2])
        if selector_type == "conversation" and len(args) == 3:
            return ConversationDeletionScope.conversation(args[2])
        if selector_type in {"date", "range"} and len(args) == 4:
            try:
                start = date.fromisoformat(args[2])
                end = date.fromisoformat(args[3])
            except ValueError as exc:
                raise ValueError("history deletion dates must be ISO dates") from exc
            return ConversationDeletionScope.date_range(start, end)
        raise ValueError("history deletion arguments are malformed")

    @staticmethod
    def _render_history_deletion_preview(
        preview: ConversationDeletionPreview,
    ) -> str:
        lines = [
            "Conversation-history deletion preview",
            f"Scope: {preview.scope.describe()}",
            f"Messages: {preview.count}",
            f"Selection digest: {preview.content_digest}",
            "Selected records:",
        ]
        lines.extend(
            (
                f"{message.history_id} | {message.occurred_at.isoformat()} | "
                f"{message.direction} | conversation {message.working_session_id} | "
                f"request {message.request_id or 'none'}"
            )
            for message in preview.messages
        )
        return "\n".join(lines)

    def _handle_history_control(
        self,
        message: InboundMessage,
        args: tuple[str, ...],
    ) -> ReceiveResult:
        """Serve bounded history reads only for the admitted operator command.

        Search returns opaque record pointers.  Content, including
        credential-like content, is released only by the exact message-id
        selector used by ``inspect`` and ``export``.
        """

        operation = args[0]
        try:
            self._append_audit(
                kind=f"conversation_history_{operation}",
                event_id=message.event_id,
                request_id=None,
                message_id=message.message_id,
                outcome="requested",
                actor="configured_operator",
                operation_type="conversation_history",
                target_category="operator_conversation",
                details={},
            )
            if operation == "search":
                matches = self.state.search_conversation_messages(
                    text=" ".join(args[1:]), limit=20
                )
                return self._dispatch_history_text(
                    message,
                    self._render_history_search(matches),
                    disposition="history_search",
                )
            if operation == "conversation":
                matches = self.state.search_conversation_messages(
                    working_session_id=args[1], limit=20
                )
                return self._dispatch_history_text(
                    message,
                    self._render_history_search(matches),
                    disposition="history_conversation",
                )

            selected = self.state.search_conversation_messages(
                history_ids=(args[1],), limit=1
            )
            if len(selected) != 1:
                return self._dispatch_history_text(
                    message,
                    "No accessible conversation message has that exact history ID.",
                    disposition=f"history_{operation}",
                )
            # The adapter's canonical export is the exact inspection payload:
            # it retains every selected field and is split only at the delivery
            # envelope, never truncated or redacted.
            payload = self.state.export_conversation_messages(history_ids=(args[1],))
        except AuditWriteError as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"conversation-history read was blocked by audit: {exc}",
            )
        except (StateStoreError, ValueError) as exc:
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                reason=f"conversation-history read failed: {exc}",
            )
        return self._dispatch_history_export(
            message,
            payload,
            disposition=f"history_{operation}",
        )
