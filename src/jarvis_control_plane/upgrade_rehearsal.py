"""Offline pinned upgrade and rollback rehearsal for manual administration."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .adapters import SQLiteDurableStateStore
from .administrative_backup import BackupError, restore_backup
from .deployment import BundleValidationError, validate_configuration, verify_bundle
from .models import OutboundAttemptStatus
from .sessions import (
    DispatchStatus,
    SQLiteWorkingSessionStore,
    interrupt_for_restart,
)


class UpgradeRehearsalError(RuntimeError):
    """The isolated upgrade rehearsal could not be completed safely."""


@dataclass(frozen=True, slots=True)
class UpgradeRehearsalReport:
    outcome: str
    admission_stopped: bool
    ingress_claims: int
    requests_interrupted: int
    pending_actions_invalidated: int
    dispatch_not_started: int
    dispatch_unknown: int
    outbound_not_started: int
    outbound_unknown: int
    candidate_state: Path
    rollback_state: Path | None
    host_mutations: tuple[str, ...] = ()


def _bundle(release: Path) -> Path:
    bundle = release / "deployment"
    return bundle if bundle.is_dir() else release


def _verify_release(release: Path, configuration: Path) -> object:
    return verify_bundle(
        _bundle(release), configuration=configuration, source_root=release
    )


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
    if replacement not in Path(__file__).resolve().parents:
        raise UpgradeRehearsalError(
            "replacement release must run its own rehearsal module"
        )

    try:
        validate_configuration(tomllib.loads(config.read_text(encoding="utf-8")))
        _verify_release(previous, config)
        _verify_release(replacement, config)
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
            artifact_lock=lock,
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
    stats = (0, 0, 0, 0, 0, 0, 0)
    rollback_state: Path | None = None
    outcome = "rehearsed"
    try:
        stats = _reconcile_known_window(
            state_path,
            session_path=session_path,
            start=start,
            end=end,
        )
        if force_failure:
            raise UpgradeRehearsalError("forced rehearsal failure")
    except Exception:  # noqa: BLE001 - every candidate failure must rehearse rollback
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

    return UpgradeRehearsalReport(
        outcome=outcome,
        admission_stopped=True,
        ingress_claims=stats[0],
        requests_interrupted=stats[1],
        pending_actions_invalidated=stats[2],
        dispatch_not_started=stats[3],
        dispatch_unknown=stats[4],
        outbound_not_started=stats[5],
        outbound_unknown=stats[6],
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
    created_at = datetime.fromisoformat(manifest["created_at"]).astimezone(UTC)
    if (
        manifest.get("kind") != "pre-change"
        or created_at < admission_stopped_at
        or manifest.get("release", {}).get("revision")
        != previous_lock.get("application", {}).get("git_revision")
    ):
        raise UpgradeRehearsalError(
            "pre-change backup does not follow the documented admission stop"
        )


def _reconcile_known_window(
    database: Path,
    *,
    session_path: Path,
    start: datetime,
    end: datetime,
) -> tuple[int, int, int, int, int, int, int]:
    state = SQLiteDurableStateStore(database)
    sessions = SQLiteWorkingSessionStore(session_path)
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
        window = (start.isoformat(), end.isoformat())
        ingress_before = int(
            state.connection.execute(
                "SELECT COUNT(*) FROM ingress_claims WHERE claimed_at BETWEEN ? AND ?",
                window,
            ).fetchone()[0]
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
        if any(
            not item.attempt_present
            or item.outbox_present
            != (
                item.status
                in {
                    OutboundAttemptStatus.UNATTEMPTED.value,
                    OutboundAttemptStatus.ATTEMPTED.value,
                }
            )
            for item in projections
        ):
            raise UpgradeRehearsalError("outbound recovery state is inconsistent")
        if any(
            item.reserved_at is None
            or not start
            <= datetime.fromisoformat(item.reserved_at).astimezone(UTC)
            <= end
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
            dispatch_not_started = sum(
                item.status is DispatchStatus.UNATTEMPTED
                for item in session.action_outbox
                if item.is_open
            )
            dispatch_unknown = sum(
                item.status is DispatchStatus.ATTEMPTED
                for item in session.action_outbox
                if item.is_open
            )
            transition = interrupt_for_restart(session, now=end)
            sessions.compare_and_set(session, transition.state)
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
        reconciled = state.reconcile_outbound_conversation_attempts(interrupted_at=end)
        ingress_after = int(
            state.connection.execute(
                "SELECT COUNT(*) FROM ingress_claims WHERE claimed_at BETWEEN ? AND ?",
                window,
            ).fetchone()[0]
        )
        if ingress_after != ingress_before:
            raise UpgradeRehearsalError("maintenance admission stop was not preserved")
        return (
            ingress_before,
            len(requests),
            pending_actions,
            dispatch_not_started,
            dispatch_unknown,
            sum(
                item.status is OutboundAttemptStatus.NOT_STARTED for item in reconciled
            ),
            sum(item.status is OutboundAttemptStatus.UNKNOWN for item in reconciled),
        )
    finally:
        sessions.close()
        state.close()


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
            force_failure=args.force_failure,
        )
    except UpgradeRehearsalError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
