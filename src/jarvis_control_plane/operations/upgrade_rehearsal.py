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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..administrative_backup import BackupError, restore_backup
from ..deployment import BundleValidationError, validate_configuration, verify_bundle
from .release_migrations import migrate_release_databases
from .upgrade_reconciliation import (  # noqa: F401
    UpgradeRehearsalError,
    _outbound_inconsistency,
    _reconcile_known_window,
    _ReconciliationStats,
    _restart_evidence,
)


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
