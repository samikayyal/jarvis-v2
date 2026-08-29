"""Audit event schemas and cross-field semantic validation rules."""

from __future__ import annotations

from collections.abc import Mapping

_ADMISSION_REJECTION_REASONS = frozenset(
    {
        "blank_text",
        "not_direct_message",
        "self_message",
        "text_too_large",
        "unauthorized_chat",
        "unauthorized_operator",
        "unresolved_identity",
        "unsupported_event_type",
        "unsupported_message_type",
        "wrong_session",
    }
)
_LIFECYCLE_PHASES = frozenset({"audit_gate", "completed", "orchestration", "outbound"})
_LIFECYCLE_STATUSES = frozenset(
    {"accepted", "blocked", "completed", "failed", "replying", "unknown"}
)
_OUTBOUND_RESULTS = frozenset(
    {
        "accepted",
        "failed",
        "outbound_failed",
        "outbound_unknown",
        "pending",
        "reply_sent",
        "trace_unavailable",
        "unknown",
    }
)
_AUDIT_DETAIL_SCHEMAS: dict[str, dict[str, frozenset[str] | None]] = {
    "service_restart": {
        "interrupted_requests": None,
        "interrupted_ingress": None,
        "outbound_not_started": None,
        "outbound_unknown": None,
    },
    "restart_inconsistency": {
        "count": None,
        "reason": frozenset(
            {
                "attempt_outbox_request_mismatch",
                "open_attempt_without_outbox",
                "outbox_without_attempt",
                "terminal_attempt_with_outbox",
                "unsupported_attempt_status",
            }
        ),
        "state": frozenset({"administrative_degraded"}),
    },
    "action_outcome": {
        "channel": frozenset({"controlled"}),
        "command": None,
        "reason": None,
        "result": frozenset(
            {"accepted", "completed", "failed", "not_started", "rejected", "unknown"}
        ),
    },
    "action_cancellation": {
        "action": None,
        "dispatch_state": frozenset({"cancelled", "unknown"}),
        "execution_status": frozenset({"not_started", "stopped", "unknown"}),
    },
    "inbound_admitted": {
        "channel": frozenset({"direct_text"}),
        "phase": frozenset({"admission"}),
    },
    "inbound_malformed": {"reason": frozenset({"malformed_envelope"})},
    "inbound_rejected": {"reason": _ADMISSION_REJECTION_REASONS},
    "orchestration_result": {
        "adapter": frozenset({"controlled", "agents_sdk_responses"}),
        "model": frozenset({"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}),
        "reasoning": frozenset({"none", "low", "medium", "high", "xhigh", "max"}),
        "result": frozenset({"failed"}),
        "state": frozenset({"unavailable"}),
    },
    "outbound_attempt": {
        "channel": frozenset({"controlled_outbound"}),
        "destination": frozenset({"configured_operator"}),
    },
    "outbound_completion": {
        "result": _OUTBOUND_RESULTS | frozenset({"not_sent"}),
        "state": frozenset({"unavailable"}),
    },
    "outbound_result": {
        "channel": frozenset({"controlled_outbound"}),
        "result": _OUTBOUND_RESULTS,
    },
    "request_accepted": {
        "phase": frozenset({"orchestration"}),
        "model": frozenset({"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}),
        "reasoning": frozenset({"none", "low", "medium", "high", "xhigh", "max"}),
    },
    "request_lifecycle": {
        "phase": _LIFECYCLE_PHASES,
        "status": _LIFECYCLE_STATUSES,
    },
    "lifecycle": {
        "phase": _LIFECYCLE_PHASES,
        "status": _LIFECYCLE_STATUSES,
    },
    "pending_action": {
        "action": None,
        "permission_id": None,
        "state": frozenset(
            {
                "frozen",
                "approved",
                "rejected",
                "expired",
                "dispatched",
                "dispatch_failed",
            }
        ),
    },
    "durable_memory_access": {
        "operation": frozenset({"list", "search", "inspect", "use"}),
        "target": None,
    },
    "durable_memory_invalid": {
        "operation": frozenset({"invalid"}),
    },
    "durable_memory_mutation": {
        "operation": frozenset({"remember", "replace", "forget"}),
    },
    "durable_memory_dispatch": {
        "action": None,
        "operation": frozenset({"remember", "replace", "forget"}),
    },
    "working_session_migration": {
        "count": None,
        "state": frozenset({"legacy_permissions_invalidated"}),
    },
    "conversation_history_deletion_preview": {
        "scope": frozenset({"message", "conversation", "date", "range"}),
    },
    "conversation_history_deletion_attempt": {
        "action": None,
    },
    "conversation_history_deletion_result": {
        "action": None,
        "result": frozenset({"completed", "failed", "unknown"}),
    },
}
_AUDIT_EVENT_RULES: dict[str, tuple[str, str, str, frozenset[str], frozenset[str]]] = {
    "service_restart": (
        "working_session",
        "working_session",
        "control_plane",
        frozenset({"interrupted"}),
        frozenset({"recorded"}),
    ),
    "restart_inconsistency": (
        "state_recovery",
        "durable_state",
        "control_plane",
        frozenset({"degraded"}),
        frozenset({"recorded"}),
    ),
    "inbound_admitted": (
        "inbound_admission",
        "messaging_gateway",
        "configured_operator",
        frozenset({"accepted"}),
        frozenset({"accepted"}),
    ),
    "inbound_malformed": (
        "inbound_admission",
        "messaging_gateway",
        "transport",
        frozenset({"rejected"}),
        frozenset({"rejected"}),
    ),
    "inbound_rejected": (
        "inbound_admission",
        "messaging_gateway",
        "transport",
        frozenset({"rejected"}),
        frozenset({"rejected"}),
    ),
    "orchestration_result": (
        "orchestration",
        "control_plane",
        "controlled_orchestration",
        frozenset({"completed", "failed", "unavailable"}),
        frozenset({"completed", "failed"}),
    ),
    "outbound_attempt": (
        "outbound_message",
        "operator_conversation",
        "controlled_outbound",
        frozenset({"attempted"}),
        frozenset({"attempted"}),
    ),
    "outbound_completion": (
        "outbound_message",
        "operator_conversation",
        "controlled_outbound",
        frozenset(
            {
                "not_sent",
                "outbound_failed",
                "outbound_unknown",
                "pending",
                "reply_sent",
                "trace_unavailable",
            }
        ),
        frozenset({"failed", "unknown", "accepted", "pending"}),
    ),
    "outbound_result": (
        "outbound_message",
        "operator_conversation",
        "controlled_outbound",
        frozenset({"accepted", "failed", "pending", "unknown"}),
        frozenset({"accepted", "failed", "pending", "unknown"}),
    ),
    "request_accepted": (
        "request_lifecycle",
        "control_plane",
        "configured_operator",
        frozenset({"accepted"}),
        frozenset({"accepted"}),
    ),
    "request_lifecycle": (
        "request_lifecycle",
        "control_plane",
        "control_plane",
        frozenset(
            {
                "accepted",
                "audit_unavailable",
                "blocked",
                "completed",
                "failed",
                "orchestration_failed",
                "outbound_failed",
                "outbound_unknown",
                "read_unavailable",
                "reply_sent",
                "replying",
                "trace_unavailable",
                "unknown",
            }
        ),
        _LIFECYCLE_STATUSES,
    ),
    "pending_action": (
        "approval_gated_action",
        "pending_action",
        "control_plane",
        frozenset(
            {
                "pending",
                "approved",
                "rejected",
                "expired",
                "dispatched",
                "dispatch_failed",
            }
        ),
        frozenset({"pending", "accepted", "completed", "failed"}),
    ),
    "durable_memory_access": (
        "durable_memory_read",
        "durable_assistant_memory",
        "configured_operator",
        frozenset({"requested"}),
        frozenset({"requested"}),
    ),
    "durable_memory_invalid": (
        "durable_memory",
        "durable_assistant_memory",
        "configured_operator",
        frozenset({"rejected"}),
        frozenset({"rejected"}),
    ),
    "durable_memory_mutation": (
        "durable_memory_mutation",
        "durable_assistant_memory",
        "configured_operator",
        frozenset({"requested"}),
        frozenset({"requested"}),
    ),
    "durable_memory_dispatch": (
        "durable_memory_mutation",
        "durable_assistant_memory",
        "control_plane",
        frozenset({"attempted", "completed", "failed", "unknown", "not_started"}),
        frozenset({"not_started", "completed", "failed", "unknown"}),
    ),
    "working_session_migration": (
        "working_session",
        "working_session",
        "control_plane",
        frozenset({"migrated"}),
        frozenset({"recorded"}),
    ),
    "conversation_history_deletion_preview": (
        "conversation_history_delete",
        "operator_conversation",
        "configured_operator",
        frozenset({"requested"}),
        frozenset({"requested"}),
    ),
    "conversation_history_deletion_attempt": (
        "conversation_history_delete",
        "operator_conversation",
        "control_plane",
        frozenset({"attempted"}),
        frozenset({"attempted"}),
    ),
    "conversation_history_deletion_result": (
        "conversation_history_delete",
        "operator_conversation",
        "control_plane",
        frozenset({"completed", "failed", "unknown"}),
        frozenset({"completed", "failed", "unknown", "not_started"}),
    ),
}


def _validate_audit_event_semantics(
    *,
    kind: str,
    operation_type: str | None,
    target_category: str | None,
    actor: str,
    outcome: str,
    execution_status: str | None,
    details: Mapping[str, str],
) -> None:
    rule = _AUDIT_EVENT_RULES.get(kind)
    if rule is not None:
        expected_operation, expected_target, expected_actor, outcomes, statuses = rule
        if operation_type is not None and operation_type != expected_operation:
            raise ValueError("audit operation type does not match its event kind")
        if target_category is not None and target_category != expected_target:
            raise ValueError("audit target category does not match its event kind")
        if actor != expected_actor:
            raise ValueError("audit actor does not match its event kind")
        if outcome not in outcomes:
            raise ValueError("audit outcome does not match its event kind")
        if execution_status is not None and execution_status not in statuses:
            raise ValueError("audit execution status does not match its event kind")

    detail_result = details.get("result")
    if kind in {"outbound_result", "outbound_completion"} and detail_result is not None:
        expected = {
            "pending": ("pending", "pending"),
            "accepted": ("accepted", "accepted"),
            "failed": ("failed", "failed"),
            "unknown": ("unknown", "unknown"),
            "not_sent": ("not_sent", "failed"),
            "outbound_failed": ("outbound_failed", "failed"),
            "outbound_unknown": ("outbound_unknown", "unknown"),
            "reply_sent": ("reply_sent", "accepted"),
            "trace_unavailable": ("trace_unavailable", "failed"),
        }.get(detail_result)
        if (
            expected is None
            or outcome != expected[0]
            or (kind == "outbound_result" and execution_status != expected[1])
            or (
                kind == "outbound_completion"
                and execution_status is not None
                and execution_status != expected[1]
            )
        ):
            raise ValueError("outbound result fields are contradictory")
    if kind == "request_lifecycle":
        status = details.get("status")
        if status is not None and execution_status != status:
            raise ValueError("request lifecycle fields are contradictory")
        expected_outcomes = {
            "accepted": "accepted",
            "blocked": "audit_unavailable",
            "failed": None,
            "replying": "replying",
            "unknown": "outbound_unknown",
        }
        expected_outcome = expected_outcomes.get(status)
        if expected_outcome is not None and outcome != expected_outcome:
            raise ValueError("request lifecycle outcome does not match its status")
        if status == "completed" and outcome not in {
            "read_unavailable",
            "reply_sent",
        }:
            raise ValueError("request lifecycle outcome does not match its status")
        if status == "failed" and outcome not in {
            "failed",
            "orchestration_failed",
            "outbound_failed",
            "trace_unavailable",
        }:
            raise ValueError("request lifecycle outcome does not match its status")
    if kind == "conversation_history_deletion_result":
        result = details.get("result")
        if result is not None and result != outcome:
            raise ValueError("conversation deletion result fields are contradictory")
        if execution_status not in {
            "completed",
            "failed",
            "unknown",
            "not_started",
        }:
            raise ValueError("conversation deletion execution status is invalid")
        if execution_status != "not_started" and execution_status != outcome:
            raise ValueError("conversation deletion result status is contradictory")
