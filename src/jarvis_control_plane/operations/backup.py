"""Manual administrative backup and isolated restore tooling for Jarvis."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKUP_SCHEMA_VERSION = 1
DATABASES = (
    ("state", "state", "state.sqlite3", "request_state"),
    ("sessions", "state", "sessions.sqlite3", "working_session_current"),
    ("audit", "audit", "audit.sqlite3", "audit_evidence"),
    ("traces", "traces", "traces.sqlite3", "diagnostic_traces"),
    ("google_traces", "google_traces", "google.sqlite3", "diagnostic_traces"),
    (
        "deleted_conversations",
        "deleted_conversations",
        "deleted-conversations.sqlite3",
        "deleted_messages",
    ),
)
DEFAULT_ROOTS = {
    "state": Path("/var/lib/jarvis/state"),
    "audit": Path("/var/lib/jarvis/audit"),
    "traces": Path("/var/lib/jarvis/traces"),
    "google_traces": Path("/var/lib/jarvis/google-traces"),
    "deleted_conversations": Path("/var/lib/jarvis/deleted-conversations"),
}
PROTECTED_ADMIN_ROOTS = tuple(DEFAULT_ROOTS.values()) + (
    Path("/var/lib/jarvis/vault"),
    Path("/etc/jarvis"),
    Path("/run/credentials"),
    Path("/run/protocol"),
    Path("/run/jarvis/deleted-archive-ipc"),
)
METADATA_FILES = (
    ("configuration", "configuration.toml"),
    ("artifact_lock", "artifacts.lock.json"),
    ("compose_manifest", "compose.yaml"),
    ("image_digests", "image-digests.json"),
)


class BackupError(RuntimeError):
    """The administrative backup or isolated restore could not be verified."""


def create_backup(
    *,
    destination: str | Path,
    kind: str,
    configuration: str | Path,
    artifact_lock: str | Path,
    compose_manifest: str | Path | None = None,
    image_digests: str | Path | None = None,
    roots: Mapping[str, str | Path] = DEFAULT_ROOTS,
    now: datetime | None = None,
    runner: Callable[..., object] | None = None,
) -> Path:
    """Create one internally consistent, credential-excluding backup snapshot."""

    if kind not in {"nightly", "pre-change"}:
        raise BackupError("backup kind must be nightly or pre-change")
    config_path = _regular_file(configuration, "configuration")
    lock_path = _regular_file(artifact_lock, "artifact lock")
    metadata_sources = {
        "configuration": config_path,
        "artifact_lock": lock_path,
        "compose_manifest": _regular_file(
            compose_manifest or lock_path.parent / "compose.yaml", "Compose manifest"
        ),
        "image_digests": _regular_file(
            image_digests or lock_path.parent / "image-digests.json", "image digests"
        ),
    }
    metadata_ownership = {
        name: _ownership(path) for name, path in metadata_sources.items()
    }

    resolved_roots: dict[str, Path] = {}
    for root_name in DEFAULT_ROOTS:
        if root_name not in roots:
            raise BackupError(f"missing backup root: {root_name}")
        raw_root = Path(roots[root_name]).expanduser()
        root = raw_root.resolve()
        if not root.is_dir() or raw_root.is_symlink():
            raise BackupError(f"backup root is unavailable: {root_name}")
        resolved_roots[root_name] = root

    destination_path = Path(destination).expanduser().resolve()
    protected_roots = {
        *resolved_roots.values(),
        *map(Path.resolve, PROTECTED_ADMIN_ROOTS),
    }
    if any(_within(destination_path, root) for root in protected_roots):
        raise BackupError("backup destination must be outside Jarvis data roots")
    destination_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    get_effective_uid = getattr(os, "geteuid", None)
    destination_stat = destination_path.stat()
    if get_effective_uid is not None and (
        destination_stat.st_uid != get_effective_uid()
        or stat.S_IMODE(destination_stat.st_mode) != 0o700
    ):
        raise BackupError(
            "backup destination must have the effective owner and mode 0700"
        )
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    snapshot = destination_path / (timestamp.strftime("%Y%m%dT%H%M%S.%fZ") + f"-{kind}")
    if snapshot.exists():
        raise BackupError("backup snapshot already exists")

    staging = Path(tempfile.mkdtemp(prefix=".partial-", dir=destination_path))
    try:
        staging.chmod(0o700)
        metadata_dir = staging / "metadata"
        metadata_dir.mkdir(mode=0o700)
        for name, filename in METADATA_FILES:
            shutil.copyfile(metadata_sources[name], metadata_dir / filename)
            _private_file(metadata_dir / filename)
        _config, lock, release = _metadata(
            metadata_dir / "configuration.toml",
            metadata_dir / "artifacts.lock.json",
        )
        _validate_deployment_metadata(
            metadata_dir / "compose.yaml",
            metadata_dir / "image-digests.json",
            activated_image_ids=(
                _activated_image_ids(metadata_sources["compose_manifest"], runner)
                if runner is not None
                else None
            ),
        )

        root_metadata = {
            name: _ownership(root) for name, root in resolved_roots.items()
        }
        sources = [
            (
                name,
                root_name,
                filename,
                required_table,
                _regular_file(resolved_roots[root_name] / filename, f"{name} database"),
            )
            for name, root_name, filename, required_table in DATABASES
        ]
        databases: list[dict[str, Any]] = []
        copied_databases = []
        with _consistent_snapshot([source for *_, source in sources]) as identities:
            for index, (name, root_name, filename, required_table, source) in enumerate(
                sources
            ):
                copied = staging / "data" / root_name / filename
                copied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _sqlite_backup(source, copied, identities[index])
                _private_file(copied)
                copied_databases.append(
                    (
                        name,
                        root_name,
                        filename,
                        required_table,
                        copied,
                        _ownership(source, identities[index]),
                    )
                )

        for (
            name,
            root_name,
            filename,
            required_table,
            copied,
            source_ownership,
        ) in copied_databases:
            schema_hash = _verify_database(copied, required_table)
            if schema_hash != lock["database_schemas"][name]:
                raise BackupError(f"database schema compatibility failed: {name}")
            databases.append(
                {
                    "name": name,
                    "root": root_name,
                    "filename": filename,
                    "path": copied.relative_to(staging).as_posix(),
                    "sha256": _sha256(copied),
                    "schema_sha256": schema_hash,
                    **source_ownership,
                }
            )

        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "kind": kind,
            "created_at": timestamp.isoformat(),
            "release": release,
            "roots": root_metadata,
            "metadata": {
                name: {
                    "path": f"metadata/{filename}",
                    "sha256": _sha256(metadata_dir / filename),
                    **metadata_ownership[name],
                }
                for name, filename in METADATA_FILES
            },
            "databases": databases,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _private_file(manifest_path)
        _sync_tree(staging)
        staging.rename(snapshot)
        _sync_directory(destination_path)
        return snapshot
    except (BackupError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError("backup creation failed") from exc


def restore_backup(
    *,
    snapshot: str | Path,
    target: str | Path,
    configuration: str | Path,
    artifact_lock: str | Path,
) -> Path:
    """Verify a snapshot and restore it into a new, isolated path only."""

    snapshot_path = Path(snapshot).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    if target_path.exists():
        raise BackupError("isolated restore target must not already exist")
    if not snapshot_path.is_dir() or snapshot_path.is_symlink():
        raise BackupError("backup snapshot is unavailable")
    if _within(target_path, snapshot_path):
        raise BackupError("isolated restore target must be outside the snapshot")

    manifest = _read_manifest(snapshot_path / "manifest.json")
    _validate_manifest(manifest)
    current_config, _lock, current_release = _metadata(
        _regular_file(configuration, "configuration"),
        _regular_file(artifact_lock, "artifact lock"),
    )
    active_roots = {
        Path(path).expanduser().resolve()
        for path in current_config.get("paths", {}).values()
        if isinstance(path, str) and path.startswith("/")
    } | {path.resolve() for path in PROTECTED_ADMIN_ROOTS}
    if any(_within(target_path, root) for root in active_roots):
        raise BackupError("isolated restore target must be outside active data roots")
    if not _release_compatible(manifest["release"], current_release):
        raise BackupError("release compatibility check failed")
    for item in manifest["databases"]:
        if item["schema_sha256"] != _lock["database_schemas"][item["name"]]:
            raise BackupError(f"database schema compatibility failed: {item['name']}")
    _trusted_admin_directory(target_path.parent, "restore parent")
    try:
        target_path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise BackupError("isolated restore target must not already exist") from exc
    staging = target_path
    marker = staging / ".restore-in-progress"
    try:
        marker.touch(mode=0o600)
        for item in manifest["databases"]:
            source = _snapshot_member(snapshot_path, item["path"])
            restored = staging / item["path"]
            restored.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, restored)
            _private_file(restored)
        metadata_dir = staging / "metadata"
        metadata_dir.mkdir(mode=0o700)
        for key, filename in METADATA_FILES:
            source = _snapshot_member(snapshot_path, manifest["metadata"][key]["path"])
            shutil.copyfile(source, metadata_dir / filename)
            _private_file(metadata_dir / filename)
        shutil.copyfile(
            _snapshot_member(snapshot_path, "manifest.json"), staging / "manifest.json"
        )
        _private_file(staging / "manifest.json")
        if _read_manifest(staging / "manifest.json") != manifest:
            raise BackupError("backup manifest changed during restore")
        _verify_snapshot_files(staging, manifest)

        for item in manifest["databases"]:
            _apply_ownership(staging / item["path"], item)
        for root_name, ownership in manifest["roots"].items():
            _apply_ownership(staging / "data" / root_name, ownership)
        for key, filename in METADATA_FILES:
            _apply_ownership(metadata_dir / filename, manifest["metadata"][key])
        _verify_restored_ownership(staging, manifest)
        marker.unlink()
        return target_path
    except (BackupError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError("isolated restore failed") from exc


from .backup_support import (  # noqa: F401
    _activated_image_ids,
    _apply_ownership,
    _compose_json_rows,
    _consistent_snapshot,
    _file_identity,
    _metadata,
    _ownership,
    _private_file,
    _read_manifest,
    _regular_file,
    _release_compatible,
    _required_table,
    _sha256,
    _snapshot_member,
    _sqlite_backup,
    _sync_directory,
    _sync_tree,
    _trusted_admin_directory,
    _validate_deployment_metadata,
    _validate_file_record,
    _validate_manifest,
    _validate_ownership,
    _verify_audit_records,
    _verify_database,
    _verify_restored_ownership,
    _verify_snapshot_files,
    _within,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--kind", choices=("nightly", "pre-change"), required=True)
    create.add_argument("--destination", type=Path, default=Path("/var/backups/jarvis"))
    create.add_argument(
        "--configuration", type=Path, default=Path("/etc/jarvis/jarvis.toml")
    )
    create.add_argument("--artifact-lock", type=Path, required=True)
    create.add_argument("--compose-manifest", type=Path)
    create.add_argument("--image-digests", type=Path)
    for name, default in DEFAULT_ROOTS.items():
        create.add_argument(f"--{name.replace('_', '-')}", type=Path, default=default)
    restore = commands.add_parser("restore")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("target", type=Path)
    restore.add_argument(
        "--configuration", type=Path, default=Path("/etc/jarvis/jarvis.toml")
    )
    restore.add_argument("--artifact-lock", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            roots = {name: getattr(args, name) for name in DEFAULT_ROOTS}
            result = create_backup(
                destination=args.destination,
                kind=args.kind,
                configuration=args.configuration,
                artifact_lock=args.artifact_lock,
                compose_manifest=args.compose_manifest,
                image_digests=args.image_digests,
                roots=roots,
                runner=subprocess.run,
            )
        else:
            result = restore_backup(
                snapshot=args.snapshot,
                target=args.target,
                configuration=args.configuration,
                artifact_lock=args.artifact_lock,
            )
    except BackupError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
