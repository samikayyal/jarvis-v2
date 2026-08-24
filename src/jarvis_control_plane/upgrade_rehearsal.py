"""Offline pinned upgrade and rollback rehearsal for manual administration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .adapters import (
    SQLiteAuditBoundary,
    SQLiteDurableStateStore,
)
from .administrative_backup import BackupError, restore_backup
from .deployment import BundleValidationError, validate_configuration, verify_bundle
from .models import (
    AuditEvidence,
    OutboundAttemptRecoveryProjection,
    OutboundAttemptStatus,
)
from .release_migrations import migrate_release_databases
from .sessions import (
    DispatchStatus,
    SQLiteWorkingSessionStore,
    interrupt_for_restart,
)


class UpgradeRehearsalError(RuntimeError):
    """The isolated upgrade rehearsal could not be completed safely."""


class _ForcedRehearsalFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UpgradeRehearsalReport:
    outcome: str
    admission_stopped: bool
    ingress_claims: int
    ingress_interrupted: int
    requests_interrupted: int
    pending_actions_invalidated: int
    dispatch_not_started: int
    dispatch_unknown: int
    outbound_not_started: int
    outbound_unknown: int
    candidate_state: Path
    rollback_state: Path | None
    host_mutations: tuple[str, ...] = ()


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


def _bundle(release: Path) -> Path:
    bundle = release / "deployment"
    return bundle if bundle.is_dir() else release


def _python(release: Path) -> Path:
    executable = (
        release / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if not executable.is_file():
        raise UpgradeRehearsalError("release Python runtime is unavailable")
    return executable.absolute()


def _verify_previous_release(release: Path, configuration: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(release / "src")
    result = subprocess.run(
        (
            str(_python(release)),
            "-m",
            "jarvis_control_plane.deployment",
            str(_bundle(release)),
            "--configuration",
            str(configuration),
            "--source-root",
            str(release),
        ),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    if result.returncode:
        raise UpgradeRehearsalError("previous release artifact validation failed")


def rehearse_upgrade(
    *,
    previous_release: str | Path,
    replacement_release: str | Path,
    configuration: str | Path,
    snapshot: str | Path,
    workspace: str | Path,
    admission_stopped_at: datetime,
    window_start: datetime,
    window_end: datetime,
    history_export: str | Path,
    force_failure: bool = False,
) -> UpgradeRehearsalReport:
    """Rehearse one replacement and rollback entirely below a new workspace."""

    if (
        admission_stopped_at.tzinfo is None
        or window_start.tzinfo is None
        or window_end.tzinfo is None
    ):
        raise UpgradeRehearsalError(
            "maintenance and reconciliation times must be timezone-aware"
        )
    stopped_at = admission_stopped_at.astimezone(UTC)
    start = window_start.astimezone(UTC)
    end = window_end.astimezone(UTC)
    if start > end:
        raise UpgradeRehearsalError("message reconciliation window is invalid")

    previous = Path(previous_release).resolve()
    replacement = Path(replacement_release).resolve()
    config = Path(configuration).resolve()
    lock = _bundle(replacement) / "artifacts.lock.json"
    previous_lock = _bundle(previous) / "artifacts.lock.json"
    target = Path(workspace).resolve()
    if target.exists():
        raise UpgradeRehearsalError("rehearsal workspace must not already exist")
    if target.is_relative_to(previous) or target.is_relative_to(replacement):
        raise UpgradeRehearsalError("rehearsal workspace must be outside releases")
    if replacement not in Path(__file__).resolve().parents or Path(
        sys.executable
    ).absolute() != _python(replacement):
        raise UpgradeRehearsalError(
            "replacement release must run its own rehearsal module"
        )

    try:
        validate_configuration(tomllib.loads(config.read_text(encoding="utf-8")))
        _verify_previous_release(previous, config)
        verify_bundle(
            _bundle(replacement), configuration=config, source_root=replacement
        )
        _validate_maintenance_snapshot(
            Path(snapshot).resolve(),
            admission_stopped_at=stopped_at,
            previous_artifact_lock=previous_lock,
        )
        target.mkdir(mode=0o700)
        candidate = restore_backup(
            snapshot=snapshot,
            target=target / "candidate-state",
            configuration=config,
            artifact_lock=previous_lock,
        )
    except (
        BackupError,
        BundleValidationError,
        KeyError,
        OSError,
        sqlite3.Error,
        tomllib.TOMLDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise UpgradeRehearsalError("upgrade rehearsal validation failed") from exc

    state_path = candidate / "data" / "state" / "state.sqlite3"
    session_path = candidate / "data" / "state" / "sessions.sqlite3"
    audit_path = candidate / "data" / "audit" / "audit.sqlite3"
    stats = _ReconciliationStats()
    rollback_state: Path | None = None
    outcome = "rehearsed"
    try:
        _migrate_candidate(candidate, applied_at=end)
        _validate_candidate_schemas(candidate, lock)
        stats = _reconcile_known_window(
            state_path,
            session_path=session_path,
            audit_path=audit_path,
            history_export=Path(history_export).resolve(),
            start=start,
            end=end,
        )
        if force_failure:
            raise _ForcedRehearsalFailure
    except Exception as candidate_error:
        try:
            rollback = restore_backup(
                snapshot=snapshot,
                target=target / "rollback-state",
                configuration=config,
                artifact_lock=previous_lock,
            )
        except BackupError as rollback_error:
            raise UpgradeRehearsalError("rollback rehearsal failed") from rollback_error
        outcome = "rolled_back"
        rollback_state = rollback / "data" / "state" / "state.sqlite3"
        if not isinstance(candidate_error, _ForcedRehearsalFailure):
            raise UpgradeRehearsalError(
                f"candidate rehearsal failed; rollback restored at {rollback}"
            ) from candidate_error

    return UpgradeRehearsalReport(
        outcome=outcome,
        admission_stopped=True,
        ingress_claims=stats.ingress_claims,
        ingress_interrupted=stats.ingress_interrupted,
        requests_interrupted=stats.requests_interrupted,
        pending_actions_invalidated=stats.pending_actions_invalidated,
        dispatch_not_started=stats.dispatch_not_started,
        dispatch_unknown=stats.dispatch_unknown,
        outbound_not_started=stats.outbound_not_started,
        outbound_unknown=stats.outbound_unknown,
        candidate_state=state_path,
        rollback_state=rollback_state,
        host_mutations=(str(target),),
    )


def _validate_maintenance_snapshot(
    snapshot: Path,
    *,
    admission_stopped_at: datetime,
    previous_artifact_lock: Path,
) -> None:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    previous_lock = json.loads(previous_artifact_lock.read_text(encoding="utf-8"))
    captured_lock = snapshot / manifest["metadata"]["artifact_lock"]["path"]
    created_at = datetime.fromisoformat(manifest["created_at"]).astimezone(UTC)
    if (
        manifest.get("kind") != "pre-change"
        or created_at < admission_stopped_at
        or manifest.get("release", {}).get("revision")
        != previous_lock.get("application", {}).get("git_revision")
        or hashlib.sha256(captured_lock.read_bytes()).hexdigest()
        != hashlib.sha256(previous_artifact_lock.read_bytes()).hexdigest()
    ):
        raise UpgradeRehearsalError(
            "pre-change backup does not follow the documented admission stop"
        )


def _migrate_candidate(candidate: Path, *, applied_at: datetime) -> None:
    migrate_release_databases(candidate, applied_at=applied_at)


def _schema_hash(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        schema = "\n".join(
            row[0] or ""
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
            )
        )
    return hashlib.sha256(schema.encode()).hexdigest()


def _validate_candidate_schemas(candidate: Path, artifact_lock: Path) -> None:
    schemas = json.loads(artifact_lock.read_text(encoding="utf-8"))["database_schemas"]
    databases = {
        "state": candidate / "data/state/state.sqlite3",
        "sessions": candidate / "data/state/sessions.sqlite3",
        "audit": candidate / "data/audit/audit.sqlite3",
        "traces": candidate / "data/traces/traces.sqlite3",
        "google_traces": candidate / "data/google_traces/google.sqlite3",
        "deleted_conversations": candidate
        / "data/deleted_conversations/deleted-conversations.sqlite3",
    }
    if any(_schema_hash(path) != schemas[name] for name, path in databases.items()):
        raise UpgradeRehearsalError("replacement database migration is incomplete")


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("previous_release", type=Path)
    parser.add_argument("replacement_release", type=Path)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument(
        "--admission-stopped-at", type=datetime.fromisoformat, required=True
    )
    parser.add_argument("--window-start", type=datetime.fromisoformat, required=True)
    parser.add_argument("--window-end", type=datetime.fromisoformat, required=True)
    parser.add_argument("--history-export", type=Path, required=True)
    parser.add_argument("--force-failure", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = rehearse_upgrade(
            previous_release=args.previous_release,
            replacement_release=args.replacement_release,
            configuration=args.configuration,
            snapshot=args.snapshot,
            workspace=args.workspace,
            admission_stopped_at=args.admission_stopped_at,
            window_start=args.window_start,
            window_end=args.window_end,
            history_export=args.history_export,
            force_failure=args.force_failure,
        )
    except UpgradeRehearsalError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
