"""Offline pinned upgrade and rollback rehearsal for manual administration."""

from __future__ import annotations

import argparse
import sqlite3
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .adapters import SQLiteDurableStateStore
from .administrative_backup import BackupError, restore_backup
from .deployment import BundleValidationError, validate_configuration, verify_bundle
from .models import OutboundAttemptStatus


class UpgradeRehearsalError(RuntimeError):
    """The isolated upgrade rehearsal could not be completed safely."""


@dataclass(frozen=True, slots=True)
class UpgradeRehearsalReport:
    outcome: str
    admission_stopped: bool
    ingress_claims: int
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
    window_start: datetime,
    window_end: datetime,
    force_failure: bool = False,
) -> UpgradeRehearsalReport:
    """Rehearse one replacement and rollback entirely below a new workspace."""

    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise UpgradeRehearsalError(
            "message reconciliation window must be timezone-aware"
        )
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

    ingress_before = not_started = unknown = 0
    try:
        validate_configuration(tomllib.loads(config.read_text(encoding="utf-8")))
        _verify_release(previous, config)
        _verify_release(replacement, config)
        target.mkdir(mode=0o700)
        candidate = restore_backup(
            snapshot=snapshot,
            target=target / "candidate-state",
            configuration=config,
            artifact_lock=lock,
        )
        state_path = candidate / "data" / "state" / "state.sqlite3"
        ingress_before, not_started, unknown = _reconcile_known_window(
            state_path,
            start=start,
            end=end,
        )
        if force_failure:
            raise UpgradeRehearsalError("forced rehearsal failure")
        return UpgradeRehearsalReport(
            outcome="rehearsed",
            admission_stopped=True,
            ingress_claims=ingress_before,
            outbound_not_started=not_started,
            outbound_unknown=unknown,
            candidate_state=state_path,
            rollback_state=None,
            host_mutations=(str(target),),
        )
    except (
        BackupError,
        BundleValidationError,
        OSError,
        sqlite3.Error,
        tomllib.TOMLDecodeError,
    ) as exc:
        raise UpgradeRehearsalError("upgrade rehearsal validation failed") from exc
    except UpgradeRehearsalError:
        candidate = target / "candidate-state" / "data" / "state" / "state.sqlite3"
        if not target.is_dir() or not candidate.is_file():
            raise
        try:
            rollback = restore_backup(
                snapshot=snapshot,
                target=target / "rollback-state",
                configuration=config,
                artifact_lock=previous_lock,
            )
        except BackupError as rollback_error:
            raise UpgradeRehearsalError("rollback rehearsal failed") from rollback_error
        return UpgradeRehearsalReport(
            outcome="rolled_back",
            admission_stopped=True,
            ingress_claims=ingress_before,
            outbound_not_started=not_started,
            outbound_unknown=unknown,
            candidate_state=candidate,
            rollback_state=rollback / "data" / "state" / "state.sqlite3",
            host_mutations=(str(target),),
        )


def _reconcile_known_window(
    database: Path,
    *,
    start: datetime,
    end: datetime,
) -> tuple[int, int, int]:
    state = SQLiteDurableStateStore(database)
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
        pending = tuple(
            item
            for item in state.list_outbound_conversation_attempt_recovery()
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
            for item in state.list_outbound_conversation_attempt_recovery()
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
            sum(
                item.status is OutboundAttemptStatus.NOT_STARTED for item in reconciled
            ),
            sum(item.status is OutboundAttemptStatus.UNKNOWN for item in reconciled),
        )
    finally:
        state.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("previous_release", type=Path)
    parser.add_argument("replacement_release", type=Path)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--configuration", type=Path, required=True)
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
