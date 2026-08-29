"""Reconcile durable state during an isolated upgrade rehearsal."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from ..adapters import SQLiteAuditBoundary, SQLiteDurableStateStore
from ..models import (
    AuditEvidence,
    OutboundAttemptRecoveryProjection,
    OutboundAttemptStatus,
)
from ..sessions import DispatchStatus, SQLiteWorkingSessionStore, interrupt_for_restart


class UpgradeRehearsalError(RuntimeError):
    """The isolated upgrade rehearsal could not be completed safely."""


@dataclass(frozen=True, slots=True)
class _ReconciliationStats:
    ingress_claims: int = 0
    ingress_interrupted: int = 0
    requests_interrupted: int = 0
    pending_actions_invalidated: int = 0
    dispatch_not_started: int = 0
    dispatch_unknown: int = 0
    outbound_not_started: int = 0
    outbound_unknown: int = 0


def _restart_evidence(
    occurred_at: datetime,
    requests: int,
    pending: Sequence[OutboundAttemptRecoveryProjection],
    missing_ingress: int,
) -> AuditEvidence:
    return AuditEvidence(
        evidence_id=f"upgrade-rehearsal-{uuid.uuid4()}",
        kind="service_restart",
        occurred_at=occurred_at,
        event_id=None,
        request_id=None,
        outcome="interrupted",
        actor="control_plane",
        operation_type="working_session",
        target_category="working_session",
        execution_status="recorded",
        details={
            "interrupted_requests": str(requests),
            "interrupted_ingress": str(missing_ingress),
            "outbound_not_started": str(
                sum(
                    item.status == OutboundAttemptStatus.UNATTEMPTED.value
                    for item in pending
                )
            ),
            "outbound_unknown": str(
                sum(
                    item.status == OutboundAttemptStatus.ATTEMPTED.value
                    for item in pending
                )
            ),
        },
    )


def _reconcile_known_window(
    database: Path,
    *,
    session_path: Path,
    audit_path: Path,
    history_export: Path,
    start: datetime,
    end: datetime,
) -> _ReconciliationStats:
    state = SQLiteDurableStateStore(database)
    sessions = SQLiteWorkingSessionStore(session_path)
    audit = SQLiteAuditBoundary(audit_path)
    try:
        primary_key = tuple(
            row[1]
            for row in sorted(
                state.connection.execute("PRAGMA table_info(ingress_claims)"),
                key=lambda row: row[5],
            )
            if row[5]
        )
        if primary_key != ("session_id", "message_id"):
            raise UpgradeRehearsalError("ingress deduplication is not durable")
        history = json.loads(history_export.read_text(encoding="utf-8"))
        if not isinstance(history, list):
            raise UpgradeRehearsalError("bounded message history export is invalid")
        keys: set[tuple[str, str]] = set()
        missing_history: list[tuple[str, str, str, datetime]] = []
        claimed_keys = {
            (claim.session_id, claim.message_id)
            for claim in state.list_ingress_claims()
        }
        for item in history:
            try:
                key = (item["session_id"], item["message_id"])
                occurred_at = datetime.fromisoformat(item["occurred_at"])
                event_id = item["event_id"]
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise UpgradeRehearsalError(
                    "bounded message history export is invalid"
                ) from exc
            if (
                not all(isinstance(value, str) and value for value in (*key, event_id))
                or occurred_at.tzinfo is None
                or key in keys
                or not start <= occurred_at.astimezone(UTC) <= end
            ):
                raise UpgradeRehearsalError("bounded message history export is invalid")
            occurred_at = occurred_at.astimezone(UTC)
            keys.add(key)
            if key not in claimed_keys:
                missing_history.append((*key, event_id, occurred_at))
        if any(
            start <= claim.claimed_at <= end
            and (claim.session_id, claim.message_id) not in keys
            for claim in state.list_ingress_claims()
        ):
            raise UpgradeRehearsalError("bounded message history export is incomplete")
        window = (start.isoformat(), end.isoformat())
        ingress_before = int(
            state.connection.execute(
                "SELECT COUNT(*) FROM ingress_claims WHERE claimed_at BETWEEN ? AND ?",
                window,
            ).fetchone()[0]
        ) + len(missing_history)
        nonterminal_ingress = tuple(
            claim
            for claim in state.list_ingress_claims()
            if claim.disposition in {"admitted", "dispatching"}
        )
        if any(not start <= claim.claimed_at <= end for claim in nonterminal_ingress):
            raise UpgradeRehearsalError(
                "unfinished ingress falls outside the known message window"
            )
        projections = state.list_outbound_conversation_attempt_recovery()
        pending = tuple(
            item
            for item in projections
            if item.status
            in {
                OutboundAttemptStatus.UNATTEMPTED.value,
                OutboundAttemptStatus.ATTEMPTED.value,
            }
        )
        open_statuses = {
            OutboundAttemptStatus.UNATTEMPTED.value,
            OutboundAttemptStatus.ATTEMPTED.value,
        }
        terminal_statuses = {
            OutboundAttemptStatus.CONFIRMED.value,
            OutboundAttemptStatus.UNKNOWN.value,
            OutboundAttemptStatus.NOT_STARTED.value,
        }
        inconsistencies = Counter(
            reason
            for item in projections
            if (
                reason := _outbound_inconsistency(
                    item, open_statuses, terminal_statuses
                )
            )
        )
        if inconsistencies:
            reason = "outbound recovery state is inconsistent"
            audit.append_batch(
                tuple(
                    AuditEvidence(
                        evidence_id=f"upgrade-rehearsal-inconsistency-{uuid.uuid4()}",
                        kind="restart_inconsistency",
                        occurred_at=end,
                        event_id=None,
                        request_id=None,
                        outcome="degraded",
                        actor="control_plane",
                        operation_type="state_recovery",
                        target_category="durable_state",
                        execution_status="recorded",
                        details={
                            "count": str(count),
                            "reason": inconsistency,
                            "state": "administrative_degraded",
                        },
                    )
                    for inconsistency, count in sorted(inconsistencies.items())
                )
            )
            state.mark_recovery_degraded(reason=reason, marked_at=end)
            raise UpgradeRehearsalError("outbound recovery state is inconsistent")
        if any(
            item.reserved_at is None
            or not start
            <= datetime.fromisoformat(item.reserved_at).astimezone(UTC)
            <= end
            or (
                item.status == OutboundAttemptStatus.ATTEMPTED.value
                and (
                    item.attempted_at is None
                    or not start
                    <= datetime.fromisoformat(item.attempted_at).astimezone(UTC)
                    <= end
                )
            )
            for item in pending
        ):
            raise UpgradeRehearsalError(
                "unfinished outbound work falls outside the known message window"
            )
        terminal_requests = {
            "blocked",
            "cancelled",
            "completed",
            "failed",
            "interrupted",
            "not_started",
            "unknown",
        }
        requests = tuple(
            request
            for request in state.list_requests()
            if request.status not in terminal_requests
        )
        if any(not start <= request.created_at <= end for request in requests):
            raise UpgradeRehearsalError(
                "unfinished request falls outside the known message window"
            )
        session = sessions.load()
        pending_actions = int(
            session is not None and session.pending_action is not None
        )
        dispatch_not_started = dispatch_unknown = 0
        if session is not None:
            live_times = tuple(
                timestamp
                for timestamp in (
                    session.active_request.created_at
                    if session.active_request is not None
                    else None,
                    session.pending_action.created_at
                    if session.pending_action is not None
                    else None,
                    *(
                        item.approved_at
                        for item in session.action_outbox
                        if item.is_open
                    ),
                )
                if timestamp is not None
            )
            if any(not start <= timestamp <= end for timestamp in live_times):
                raise UpgradeRehearsalError(
                    "unfinished session work falls outside the known message window"
                )
            dispatch_not_started = sum(
                item.status is DispatchStatus.UNATTEMPTED
                for item in session.action_outbox
                if item.is_open
            )
            dispatch_unknown = sum(
                item.status is not DispatchStatus.UNATTEMPTED
                for item in session.action_outbox
                if item.is_open
            )
            transition = interrupt_for_restart(session, now=end)
            restart_evidence = _restart_evidence(
                end, len(requests), pending, len(missing_history)
            )
            sessions.compare_and_set_with_audit(
                session, transition.state, audit=audit, evidence=restart_evidence
            )
        else:
            audit.append(
                _restart_evidence(end, len(requests), pending, len(missing_history))
            )
        for session_id, message_id, event_id, occurred_at in missing_history:
            state.claim_ingress(
                session_id=session_id,
                message_id=message_id,
                event_id=event_id,
                claimed_at=occurred_at,
                disposition="interrupted",
            )
        for request in requests:
            state.update_request(
                replace(
                    request,
                    updated_at=end,
                    status="interrupted",
                    phase="interrupted",
                    outcome="interrupted",
                    error_code="upgrade_rehearsal",
                )
            )
        ingress_interrupted = state.reconcile_ingress_restart(
            audit=audit,
            audit_evidence=AuditEvidence(
                evidence_id="upgrade-rehearsal-ingress",
                kind="service_restart",
                occurred_at=end,
                event_id=None,
                request_id=None,
                outcome="interrupted",
                actor="control_plane",
                operation_type="working_session",
                target_category="working_session",
                execution_status="recorded",
                details={"interrupted_ingress": "nonterminal"},
            ),
        )
        reconciled = state.reconcile_outbound_conversation_attempts(interrupted_at=end)
        ingress_after = int(
            state.connection.execute(
                "SELECT COUNT(*) FROM ingress_claims WHERE claimed_at BETWEEN ? AND ?",
                window,
            ).fetchone()[0]
        )
        if ingress_after != ingress_before:
            raise UpgradeRehearsalError("maintenance admission stop was not preserved")
        return _ReconciliationStats(
            ingress_claims=ingress_before,
            ingress_interrupted=ingress_interrupted,
            requests_interrupted=len(requests),
            pending_actions_invalidated=pending_actions,
            dispatch_not_started=dispatch_not_started,
            dispatch_unknown=dispatch_unknown,
            outbound_not_started=sum(
                item.status is OutboundAttemptStatus.NOT_STARTED for item in reconciled
            ),
            outbound_unknown=sum(
                item.status is OutboundAttemptStatus.UNKNOWN for item in reconciled
            ),
        )
    finally:
        audit.close()
        sessions.close()
        state.close()


def _outbound_inconsistency(
    item: OutboundAttemptRecoveryProjection,
    open_statuses: set[str],
    terminal_statuses: set[str],
) -> str | None:
    if not item.attempt_present:
        return "outbox_without_attempt"
    if item.status in open_statuses:
        if not item.outbox_present:
            return "open_attempt_without_outbox"
        if item.outbox_request_id != item.attempt_request_id:
            return "attempt_outbox_request_mismatch"
        return None
    if item.status not in terminal_statuses:
        return "unsupported_attempt_status"
    return "terminal_attempt_with_outbox" if item.outbox_present else None
