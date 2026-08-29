"""Working-session JSON serialization."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from enum import Enum

from ...domain.session_actions import (
    ActionDispatchRecord,
    PendingActionState,
    ProposalPresentationFragment,
)
from ...domain.session_core import (
    _MAX_LEGACY_PERMISSION_MIGRATION_COUNT,
    _WORKING_SESSION_SCHEMA_VERSION,
    DurableStateReferences,
    ProposalPresentationStatus,
    ReadinessState,
    ServiceReadiness,
    SessionStoreError,
)
from ...domain.session_requests import (
    ActiveRequestState,
    CommandPermissionComponent,
    CommandPermissionIdentity,
    CommandPermissionState,
)
from ...domain.session_state import WorkingSession


def _session_json(session: WorkingSession) -> str:
    """Serialize every durable field in a stable representation for CAS."""

    def default(value: object) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        raise TypeError(f"cannot serialize working-session field {type(value)!r}")

    return json.dumps(
        {
            "schema_version": _WORKING_SESSION_SCHEMA_VERSION,
            "session_id": session.session_id,
            "operator_id": session.operator_id,
            "created_at": session.created_at,
            "last_activity_at": session.last_activity_at,
            "inactivity_anchor_at": session.inactivity_anchor_at,
            "session_minutes": session.session_minutes,
            "model": session.model,
            "reasoning": session.reasoning,
            "default_model": session.default_model,
            "default_reasoning": session.default_reasoning,
            "default_session_minutes": session.default_session_minutes,
            "conversation_ref": session.conversation_ref,
            "durable_refs": {
                "operational_state_ref": session.durable_refs.operational_state_ref,
                "conversation_store_ref": session.durable_refs.conversation_store_ref,
                "durable_memory_ref": session.durable_refs.durable_memory_ref,
                "audit_ref": session.durable_refs.audit_ref,
            },
            "active_request": (
                {
                    "request_id": session.active_request.request_id,
                    "session_id": session.active_request.session_id,
                    "generation": session.active_request.generation,
                    "phase": session.active_request.phase,
                    "created_at": session.active_request.created_at,
                    "updated_at": session.active_request.updated_at,
                    "originating_message_id": session.active_request.originating_message_id,
                    "execution_host": session.active_request.execution_host,
                    "cancellation_reason": session.active_request.cancellation_reason,
                    "terminal_outcome": session.active_request.terminal_outcome,
                }
                if session.active_request is not None
                else None
            ),
            "pending_action": (
                {
                    "action_id": session.pending_action.action_id,
                    "session_id": session.pending_action.session_id,
                    "request_id": session.pending_action.request_id,
                    "kind": session.pending_action.kind,
                    "summary": session.pending_action.summary,
                    "digest": session.pending_action.digest,
                    "preview": session.pending_action.preview,
                    "payload": session.pending_action.payload,
                    "policy_disposition": session.pending_action.policy_disposition,
                    "presentation_status": session.pending_action.presentation_status,
                    "presentation_fragments": [
                        {
                            "number": fragment.number,
                            "total": fragment.total,
                            "outbound_id": fragment.outbound_id,
                            "accepted": fragment.accepted,
                        }
                        for fragment in session.pending_action.presentation_fragments
                    ],
                    "created_at": session.pending_action.created_at,
                    "expires_at": session.pending_action.expires_at,
                }
                if session.pending_action is not None
                else None
            ),
            "action_outbox": [
                {
                    "action_id": record.action_id,
                    "session_id": record.session_id,
                    "request_id": record.request_id,
                    "kind": record.kind,
                    "digest": record.digest,
                    "status": record.status,
                    "approved_at": record.approved_at,
                    "payload": record.payload,
                    "preview": record.preview,
                    "attempted_at": record.attempted_at,
                    "terminal_at": record.terminal_at,
                }
                for record in session.action_outbox
            ],
            "permissions": [
                {
                    "permission_id": item.permission_id,
                    "lifetime": item.lifetime,
                    "identity": {
                        "host": item.identity.host,
                        "cwd": item.identity.cwd,
                        "components": [
                            {
                                "executable": component.executable,
                                "arguments": component.arguments,
                                "operator_before": component.operator_before,
                                "redirections": component.redirections,
                            }
                            for component in item.identity.components
                        ],
                    },
                    "created_at": item.created_at,
                    "session_id": item.session_id,
                    "last_used_at": item.last_used_at,
                    "revoked_at": item.revoked_at,
                    "authorization_request_id": item.authorization_request_id,
                    "authorization_action_id": item.authorization_action_id,
                    "authorization_approval": item.authorization_approval,
                    "authorization_audit_id": item.authorization_audit_id,
                }
                for item in session.permissions
            ],
            "readiness": {
                "ubuntu": session.readiness.ubuntu,
                "windows": session.readiness.windows,
                "openwa": session.readiness.openwa,
                "connected_services": [
                    {"service_id": item.service_id, "state": item.state}
                    for item in session.readiness.connected_services
                ],
            },
            "cancellation_generation": session.cancellation_generation,
            "session_number": session.session_number,
            "next_request_number": session.next_request_number,
            "lifecycle": session.lifecycle,
            "last_request_id": session.last_request_id,
            "last_request_outcome": session.last_request_outcome,
            "last_terminal_at": session.last_terminal_at,
            "legacy_permissions_invalidated": session.legacy_permissions_invalidated,
        },
        default=default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse_timestamp(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _session_from_json(value: str) -> WorkingSession:
    """Deserialize a complete persisted session, failing closed on bad state."""

    try:
        payload = json.loads(value)
        if not isinstance(payload, Mapping):
            raise TypeError("persisted working session must be an object")
        schema_version = payload.get("schema_version")
        if schema_version is not None and (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in {1, _WORKING_SESSION_SCHEMA_VERSION}
        ):
            raise ValueError("unsupported working-session schema version")
        refs = payload["durable_refs"]
        readiness = payload["readiness"]
        request = payload["active_request"]
        action = payload["pending_action"]
        active_request = (
            ActiveRequestState(
                request_id=request["request_id"],
                session_id=request["session_id"],
                generation=request["generation"],
                phase=request["phase"],
                created_at=_parse_timestamp(request["created_at"]),
                updated_at=_parse_timestamp(request["updated_at"]),
                originating_message_id=request["originating_message_id"],
                execution_host=request["execution_host"],
                cancellation_reason=request["cancellation_reason"],
                terminal_outcome=request["terminal_outcome"],
            )
            if request is not None
            else None
        )
        pending_action = (
            PendingActionState(
                action_id=action["action_id"],
                session_id=action["session_id"],
                request_id=action["request_id"],
                kind=action["kind"],
                summary=action["summary"],
                digest=action.get("digest", ""),
                preview=action.get("preview"),
                payload=action.get("payload", ""),
                policy_disposition=action.get("policy_disposition"),
                presentation_status=action.get(
                    "presentation_status", ProposalPresentationStatus.PRESENTED
                ),
                presentation_fragments=tuple(
                    ProposalPresentationFragment(
                        number=item["number"],
                        total=item["total"],
                        outbound_id=item["outbound_id"],
                        accepted=item.get("accepted", True),
                    )
                    for item in action.get("presentation_fragments", ())
                ),
                created_at=_parse_timestamp(action["created_at"]),
                expires_at=_parse_timestamp(action["expires_at"]),
            )
            if action is not None
            else None
        )
        action_outbox = tuple(
            ActionDispatchRecord(
                action_id=item["action_id"],
                session_id=item["session_id"],
                request_id=item["request_id"],
                kind=item["kind"],
                digest=item["digest"],
                status=item["status"],
                approved_at=_parse_timestamp(item["approved_at"]),
                payload=item["payload"],
                preview=item["preview"],
                attempted_at=_parse_timestamp(item["attempted_at"]),
                terminal_at=_parse_timestamp(item["terminal_at"]),
            )
            for item in payload.get("action_outbox", ())
        )
        raw_permissions = payload["permissions"]
        if not isinstance(raw_permissions, (list, tuple)):
            raise TypeError("persisted permissions must be an ordered sequence")
        legacy_permissions_invalidated = payload.get(
            "legacy_permissions_invalidated", 0
        )
        if (
            isinstance(legacy_permissions_invalidated, bool)
            or not isinstance(legacy_permissions_invalidated, int)
            or not 0
            <= legacy_permissions_invalidated
            <= _MAX_LEGACY_PERMISSION_MIGRATION_COUNT
        ):
            raise ValueError("persisted permission migration count is invalid")

        # The parent Ticket 10 representation was deliberately flattened.  It
        # cannot be upgraded into an exact structured identity, so preserve
        # every other session field and invalidate only those permission rules.
        legacy_shape = schema_version == 1 or (
            schema_version is None
            and any(
                not isinstance(item, Mapping) or "identity" not in item
                for item in raw_permissions
            )
        )
        if legacy_shape:
            permissions = ()
            legacy_permissions_invalidated += len(raw_permissions)
        else:
            permissions = tuple(
                CommandPermissionState(
                    permission_id=item["permission_id"],
                    lifetime=item["lifetime"],
                    identity=CommandPermissionIdentity(
                        host=item["identity"]["host"],
                        cwd=item["identity"]["cwd"],
                        components=tuple(
                            CommandPermissionComponent(
                                executable=component["executable"],
                                arguments=tuple(component["arguments"]),
                                operator_before=component["operator_before"],
                                redirections=tuple(component["redirections"]),
                            )
                            for component in item["identity"]["components"]
                        ),
                    ),
                    created_at=_parse_timestamp(item["created_at"]),
                    session_id=item["session_id"],
                    last_used_at=_parse_timestamp(item["last_used_at"]),
                    revoked_at=_parse_timestamp(item["revoked_at"]),
                    authorization_request_id=item.get("authorization_request_id"),
                    authorization_action_id=item.get("authorization_action_id"),
                    authorization_approval=item.get("authorization_approval"),
                    authorization_audit_id=item.get("authorization_audit_id"),
                )
                for item in raw_permissions
            )
        return WorkingSession(
            session_id=payload["session_id"],
            operator_id=payload["operator_id"],
            created_at=_parse_timestamp(payload["created_at"]),
            last_activity_at=_parse_timestamp(payload["last_activity_at"]),
            inactivity_anchor_at=_parse_timestamp(payload["inactivity_anchor_at"]),
            session_minutes=payload["session_minutes"],
            model=payload["model"],
            reasoning=payload["reasoning"],
            default_model=payload["default_model"],
            default_reasoning=payload["default_reasoning"],
            # Ticket 05 persisted only the current duration.  Before Ticket 06
            # it was also the duration carried into `/new`, so retain that
            # behavior while adding the distinct future-session default.
            default_session_minutes=payload.get(
                "default_session_minutes", payload["session_minutes"]
            ),
            conversation_ref=payload["conversation_ref"],
            durable_refs=DurableStateReferences(**refs),
            active_request=active_request,
            pending_action=pending_action,
            action_outbox=action_outbox,
            permissions=permissions,
            readiness=ReadinessState(
                ubuntu=readiness["ubuntu"],
                windows=readiness["windows"],
                openwa=readiness["openwa"],
                connected_services=tuple(
                    ServiceReadiness(**item) for item in readiness["connected_services"]
                ),
            ),
            cancellation_generation=payload["cancellation_generation"],
            session_number=payload["session_number"],
            next_request_number=payload["next_request_number"],
            lifecycle=payload["lifecycle"],
            last_request_id=payload["last_request_id"],
            last_request_outcome=payload["last_request_outcome"],
            last_terminal_at=_parse_timestamp(payload["last_terminal_at"]),
            legacy_permissions_invalidated=legacy_permissions_invalidated,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SessionStoreError("persisted working session is invalid") from exc
