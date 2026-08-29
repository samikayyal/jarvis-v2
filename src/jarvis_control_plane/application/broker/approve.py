# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker approve workflow."""

from __future__ import annotations

from .support import *


class _BrokerApprovalMixin:
    @staticmethod
    def _action_ack_kind(action_kind: str) -> str | None:
        return {
            "gmail_send": "Gmail",
            "gmail_reply": "Gmail",
            "knowledge_vault_write": "knowledge vault",
        }.get(action_kind)

    def _maybe_dispatch_action_ack(
        self,
        message: InboundMessage,
        action_kind: str,
        result: ReceiveResult,
    ) -> ReceiveResult:
        """Send one terminal connector-action acknowledgement after reconciliation."""

        if (
            action_kind == "terminal"
            and result.disposition == "action_dispatched"
            and result.reason
        ):
            control_id = self.ids.new_id("action-ack")
            acknowledgement = self._dispatch_control_text(
                message,
                body=_bounded_informational_reply(result.reason, request_id=control_id),
                control_id=control_id,
                disposition=result.disposition,
            )
            if acknowledgement.disposition in {"failed", "unknown"}:
                return replace(
                    result,
                    request=acknowledgement.request,
                    reply=acknowledgement.reply,
                    reason=(
                        "terminal action completed but its result acknowledgement "
                        f"was {acknowledgement.disposition}"
                    ),
                )
            return replace(
                acknowledgement,
                request=result.request,
                reason=result.reason,
            )
        service_name = self._action_ack_kind(action_kind)
        if service_name is None or result.disposition not in {
            "action_rejected",
            "action_invalidated",
            "action_dispatched",
            "action_dispatch_failed",
            "action_dispatch_not_started",
            "action_dispatch_unavailable",
            "action_dispatch_unknown",
        }:
            return result
        if result.disposition == "action_rejected":
            body = (
                f"The {service_name} action was rejected before dispatch. No side "
                "effect occurred and no retry is needed."
            )
        elif result.disposition == "action_invalidated":
            body = (
                f"The {service_name} action was invalidated before dispatch because "
                "its connector state changed. No retry is needed."
            )
        elif result.disposition == "action_dispatched":
            body = (
                f"The approved {service_name} action completed successfully after "
                "durable reconciliation. No retry is needed."
            )
        elif result.disposition == "action_dispatch_unknown":
            body = (
                f"The approved {service_name} action has an unknown provider "
                "outcome. No retry will be attempted."
            )
        elif result.disposition == "action_dispatch_not_started":
            body = (
                f"The approved {service_name} action was not started. No retry was "
                "attempted."
            )
        else:
            body = (
                f"The approved {service_name} action failed before completion. No "
                "retry was attempted."
            )
        control_id = self.ids.new_id("action-ack")
        acknowledgement = self._dispatch_control_text(
            message,
            body=_bounded_informational_reply(body, request_id=control_id),
            control_id=control_id,
            disposition=result.disposition,
        )
        if acknowledgement.disposition in {"failed", "unknown"}:
            reason = acknowledgement.reason or "terminal acknowledgement failed"
            return replace(
                result,
                request=result.request,
                reply=acknowledgement.reply,
                reason=(
                    f"{result.reason or result.disposition}; terminal "
                    f"acknowledgement outcome was {acknowledgement.disposition}: {reason}"
                ),
            )
        return replace(
            acknowledgement,
            request=result.request,
            reason=result.reason,
        )

    def _approve_pending_action(
        self,
        message: InboundMessage,
        session: WorkingSession,
        choice: object,
    ) -> _ApprovedActionDispatch | ReceiveResult:
        """Own approval semantics before any external dispatcher is called."""

        action = session.pending_action
        request = session.active_request
        if action is None or request is None:
            return ReceiveResult(status_code=202, disposition="pending_unavailable")
        choice_value = getattr(choice, "value", choice)
        if choice_value == ApprovalChoice.REJECT.value:
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
                    approval_decision="rejected",
                    details={"action": action.action_id, "state": "rejected"},
                )
            except (AuditWriteError, SessionStoreError) as exc:
                return ReceiveResult(
                    status_code=202,
                    disposition="audit_blocked",
                    reason=f"pending action rejection was not recorded: {exc}",
                )
            return ReceiveResult(status_code=202, disposition="action_rejected")

        if action.kind != "durable_memory":
            invalidated = self._invalidate_changed_connector_action(
                message, session, action
            )
            if invalidated is not None:
                return invalidated

        transition = approve_pending_action(session, now=self.clock)
        approved_state = transition.state
        approval_decision = "approved"
        permission_id: str | None = None
        permission_to_create: CommandPermissionState | None = None
        terminal = None
        terminal_policy = None
        if action.kind == "terminal":
            frozen = FrozenActionProposal(
                action_id=action.action_id,
                request_id=action.request_id,
                kind=action.kind,
                preview=action.preview or "",
                payload=action.payload,
                digest=action.digest,
            )
            try:
                terminal = terminal_action_from_proposal(frozen)
                terminal_policy = authorize_terminal_proposal(
                    frozen, permissions=session.permissions
                )
            except (TypeError, ValueError):
                return ReceiveResult(
                    status_code=202,
                    disposition="pending_blocked",
                    reason="terminal proposal could not be revalidated before dispatch",
                )
            if action.policy_disposition == TerminalDisposition.EXACT_PERMISSION.value:
                if (
                    terminal_policy.disposition
                    is not TerminalDisposition.EXACT_PERMISSION
                    or terminal_policy.matched_permission_id is None
                ):
                    return self._reject_revoked_pending_action(
                        message,
                        session,
                        action,
                        permission_id=terminal_policy.matched_permission_id,
                    )
                permission_id = terminal_policy.matched_permission_id
                approved_state = replace(
                    approved_state,
                    permissions=tuple(
                        replace(permission, last_used_at=self.clock.now())
                        if permission.permission_id == permission_id
                        else permission
                        for permission in approved_state.permissions
                    ),
                )
        if choice_value in {
            ApprovalChoice.SESSION_PERMISSION.value,
            ApprovalChoice.PERSISTENT_PERMISSION.value,
        }:
            if action.policy_disposition not in {
                TerminalDisposition.PROTECTED_APPROVAL.value,
                TerminalDisposition.ORDINARY_APPROVAL.value,
            }:
                return ReceiveResult(status_code=202, disposition="pending_blocked")
            if terminal is None or terminal_policy is None:
                return ReceiveResult(status_code=202, disposition="pending_blocked")
            if terminal_policy.disposition not in {
                TerminalDisposition.PROTECTED_APPROVAL,
                TerminalDisposition.ORDINARY_APPROVAL,
            }:
                return ReceiveResult(status_code=202, disposition="pending_blocked")
            lifetime = (
                PermissionLifetime.SESSION
                if choice_value == ApprovalChoice.SESSION_PERMISSION.value
                else PermissionLifetime.PERSISTENT
            )
            permission_to_create = CommandPermissionState(
                permission_id=self.ids.new_id("permission"),
                lifetime=lifetime,
                identity=terminal.permission_identity,
                created_at=self.clock.now(),
                session_id=(
                    session.session_id
                    if lifetime is PermissionLifetime.SESSION
                    else None
                ),
                authorization_request_id=action.request_id,
                authorization_action_id=action.action_id,
                authorization_approval=choice_value,
            )
            permission_id = permission_to_create.permission_id
            approved_state = replace(
                approved_state,
                permissions=(*approved_state.permissions, permission_to_create),
            )
            approval_decision = choice_value
        try:
            details = {"action": action.action_id, "state": "approved"}
            if permission_id is not None:
                details["permission_id"] = permission_id
            evidence = self._audit_evidence(
                kind="pending_action",
                event_id=message.event_id,
                request_id=action.request_id,
                message_id=message.message_id,
                outcome="approved",
                actor="control_plane",
                operation_type="approval_gated_action",
                target_category="pending_action",
                approval_decision=approval_decision,
                details=details,
            )
            if permission_to_create is not None:
                approved_state = replace(
                    approved_state,
                    permissions=tuple(
                        replace(
                            permission,
                            authorization_audit_id=evidence.evidence_id,
                        )
                        if permission.permission_id
                        == permission_to_create.permission_id
                        else permission
                        for permission in approved_state.permissions
                    ),
                )
            self.working_sessions.compare_and_set_with_audit(
                session,
                approved_state,
                audit=self.audit,
                evidence=evidence,
            )
        except (AuditWriteError, SessionStoreError) as exc:
            self._close_unattempted_action(action.action_id)
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"pending action approval was not recorded: {exc}",
            )

        return _ApprovedActionDispatch(
            action=action,
            terminal=terminal,
            permission_id=permission_id,
        )
