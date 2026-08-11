"""Manual administrative backup and isolated restore tooling for Jarvis."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .deployment import BundleValidationError, validate_configuration

BACKUP_SCHEMA_VERSION = 1
DATABASES = (
    ("state", "state", "state.sqlite3", "request_state"),
    ("sessions", "state", "sessions.sqlite3", "working_session_current"),
    ("audit", "audit", "audit.sqlite3", "audit_evidence"),
    ("traces", "traces", "traces.sqlite3", "diagnostic_traces"),
    ("codex_traces", "codex_traces", "codex.sqlite3", "diagnostic_traces"),
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
    "codex_traces": Path("/var/lib/jarvis/codex-traces"),
    "google_traces": Path("/var/lib/jarvis/google-traces"),
    "deleted_conversations": Path("/var/lib/jarvis/deleted-conversations"),
}


class BackupError(RuntimeError):
    """The administrative backup or isolated restore could not be verified."""


def create_backup(
    *,
    destination: str | Path,
    kind: str,
    configuration: str | Path,
    artifact_lock: str | Path,
    roots: Mapping[str, str | Path] = DEFAULT_ROOTS,
    now: datetime | None = None,
) -> Path:
    """Create one internally consistent, credential-excluding backup snapshot."""

    if kind not in {"nightly", "pre-change"}:
        raise BackupError("backup kind must be nightly or pre-change")
    config_path = _regular_file(configuration, "configuration")
    lock_path = _regular_file(artifact_lock, "artifact lock")
    config, lock, release = _metadata(config_path, lock_path)
    del config, lock

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
    if any(_within(destination_path, root) for root in resolved_roots.values()):
        raise BackupError("backup destination must be outside Jarvis data roots")
    destination_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    snapshot = destination_path / (timestamp.strftime("%Y%m%dT%H%M%S.%fZ") + f"-{kind}")
    if snapshot.exists():
        raise BackupError("backup snapshot already exists")

    staging = Path(tempfile.mkdtemp(prefix=".partial-", dir=destination_path))
    try:
        staging.chmod(0o700)
        metadata_dir = staging / "metadata"
        metadata_dir.mkdir(mode=0o700)
        shutil.copyfile(config_path, metadata_dir / "configuration.toml")
        shutil.copyfile(lock_path, metadata_dir / "artifacts.lock.json")
        _private_file(metadata_dir / "configuration.toml")
        _private_file(metadata_dir / "artifacts.lock.json")

        root_metadata: dict[str, dict[str, int]] = {}
        databases: list[dict[str, Any]] = []
        for name, root_name, filename, required_table in DATABASES:
            root = resolved_roots[root_name]
            root_metadata.setdefault(root_name, _ownership(root))
            source = _regular_file(root / filename, f"{name} database")
            copied = staging / "data" / root_name / filename
            copied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _sqlite_backup(source, copied)
            _private_file(copied)
            schema_hash = _verify_database(copied, required_table)
            source_ownership = _ownership(source)
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
                "configuration": {
                    "path": "metadata/configuration.toml",
                    "sha256": _sha256(metadata_dir / "configuration.toml"),
                    **_ownership(config_path),
                },
                "artifact_lock": {
                    "path": "metadata/artifacts.lock.json",
                    "sha256": _sha256(metadata_dir / "artifacts.lock.json"),
                    **_ownership(lock_path),
                },
            },
            "databases": databases,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _private_file(manifest_path)
        staging.rename(snapshot)
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
    } | {path.resolve() for path in DEFAULT_ROOTS.values()}
    if any(_within(target_path, root) for root in active_roots):
        raise BackupError("isolated restore target must be outside active data roots")
    if not _release_compatible(manifest["release"], current_release):
        raise BackupError("release compatibility check failed")
    _verify_snapshot_files(snapshot_path, manifest)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target_path.name}.partial-", dir=target_path.parent)
    )
    try:
        staging.chmod(0o700)
        for item in manifest["databases"]:
            source = _snapshot_member(snapshot_path, item["path"])
            restored = staging / item["path"]
            restored.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, restored)
            _apply_ownership(restored, item)
            schema_hash = _verify_database(restored, _required_table(item["name"]))
            if schema_hash != item["schema_sha256"]:
                raise BackupError(
                    f"database schema compatibility failed: {item['name']}"
                )

        for root_name, ownership in manifest["roots"].items():
            _apply_ownership(staging / "data" / root_name, ownership)
        metadata_dir = staging / "metadata"
        metadata_dir.mkdir(mode=0o700)
        for key, filename in (
            ("configuration", "configuration.toml"),
            ("artifact_lock", "artifacts.lock.json"),
        ):
            source = _snapshot_member(snapshot_path, manifest["metadata"][key]["path"])
            shutil.copyfile(source, metadata_dir / filename)
            _apply_ownership(metadata_dir / filename, manifest["metadata"][key])
        shutil.copyfile(snapshot_path / "manifest.json", staging / "manifest.json")
        _private_file(staging / "manifest.json")
        _verify_restored_ownership(staging, manifest)
        staging.rename(target_path)
        return target_path
    except (BackupError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError("isolated restore failed") from exc


def _metadata(
    configuration: Path, artifact_lock: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    try:
        config = tomllib.loads(configuration.read_text(encoding="utf-8"))
        lock = json.loads(artifact_lock.read_text(encoding="utf-8"))
        validate_configuration(config)
        application = lock["application"]
        if application.get("name") != "jarvis-v2":
            raise BackupError("release metadata is invalid")
        if config.get("schema_version") != 1 or lock.get("schema_version") != 1:
            raise BackupError("schema compatibility check failed")
        release = {
            "id": config["release_id"],
            "version": application["version"],
            "revision": application["git_revision"],
        }
        if not all(isinstance(value, str) and value for value in release.values()):
            raise TypeError
        return config, lock, release
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
        BundleValidationError,
    ) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError("release metadata is invalid") from exc


def _sqlite_backup(source: Path, target: Path) -> None:
    uri = f"file:{quote(source.as_posix(), safe='/:')}?mode=ro"
    source_connection = sqlite3.connect(uri, uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
        target_connection.execute("PRAGMA journal_mode = DELETE")
    finally:
        target_connection.close()
        source_connection.close()


def _verify_database(database: Path, required_table: str) -> str:
    uri = f"file:{quote(database.as_posix(), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise BackupError(f"database integrity check failed: {database.name}")
        connection.execute(f'SELECT COUNT(*) FROM "{required_table}"').fetchone()
        schema = "\n".join(
            row[0] or ""
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
            )
        ).encode("utf-8")
        return hashlib.sha256(schema).hexdigest()
    except sqlite3.Error as exc:
        raise BackupError(
            f"database readability check failed: {database.name}"
        ) from exc
    finally:
        connection.close()


def _verify_snapshot_files(snapshot: Path, manifest: Mapping[str, Any]) -> None:
    for item in (*manifest["metadata"].values(), *manifest["databases"]):
        member = _snapshot_member(snapshot, item["path"])
        if _sha256(member) != item["sha256"]:
            raise BackupError(f"backup checksum mismatch: {item['path']}")
    for item in manifest["databases"]:
        schema_hash = _verify_database(
            _snapshot_member(snapshot, item["path"]), _required_table(item["name"])
        )
        if schema_hash != item["schema_sha256"]:
            raise BackupError(f"database schema compatibility failed: {item['name']}")
    snapshot_config, snapshot_lock, snapshot_release = _metadata(
        _snapshot_member(snapshot, manifest["metadata"]["configuration"]["path"]),
        _snapshot_member(snapshot, manifest["metadata"]["artifact_lock"]["path"]),
    )
    del snapshot_config, snapshot_lock
    if snapshot_release != manifest["release"]:
        raise BackupError("snapshot release metadata is inconsistent")


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupError("backup schema compatibility check failed")
    if manifest.get("kind") not in {"nightly", "pre-change"}:
        raise BackupError("backup kind is invalid")
    release = manifest.get("release")
    if not isinstance(release, dict) or not all(
        isinstance(release.get(key), str) and release[key]
        for key in ("id", "version", "revision")
    ):
        raise BackupError("backup release metadata is incomplete")
    expected_databases = {item[0]: item[1:3] for item in DATABASES}
    databases = manifest.get("databases")
    if (
        not isinstance(databases, list)
        or len(databases) != len(expected_databases)
        or {item.get("name") for item in databases if isinstance(item, dict)}
        != set(expected_databases)
    ):
        raise BackupError("backup database inventory is incomplete")
    for item in databases:
        if not isinstance(item, dict):
            raise BackupError("backup database inventory is invalid")
        expected_root, expected_filename = expected_databases[item["name"]]
        if (
            item.get("root") != expected_root
            or item.get("filename") != expected_filename
            or item.get("path") != f"data/{expected_root}/{expected_filename}"
            or not isinstance(item.get("schema_sha256"), str)
        ):
            raise BackupError("backup database inventory is invalid")
        _validate_file_record(item)
    roots = manifest.get("roots")
    if not isinstance(roots, dict) or set(roots) != set(DEFAULT_ROOTS):
        raise BackupError("backup root inventory is incomplete")
    for ownership in roots.values():
        _validate_ownership(ownership)
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict) or set(metadata) != {
        "configuration",
        "artifact_lock",
    }:
        raise BackupError("backup manifest is incomplete")
    for item in metadata.values():
        _validate_file_record(item)
    if (
        metadata["configuration"]["path"] != "metadata/configuration.toml"
        or metadata["artifact_lock"]["path"] != "metadata/artifacts.lock.json"
    ):
        raise BackupError("backup metadata inventory is invalid")


def _validate_file_record(item: object) -> None:
    if not isinstance(item, dict) or not all(
        isinstance(item.get(key), str) and item[key] for key in ("path", "sha256")
    ):
        raise BackupError("backup file metadata is invalid")
    _validate_ownership(item)


def _validate_ownership(item: object) -> None:
    if not isinstance(item, dict) or any(
        isinstance(item.get(key), bool) or not isinstance(item.get(key), int)
        for key in ("mode", "uid", "gid")
    ):
        raise BackupError("backup ownership metadata is invalid")


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            _regular_file(path, "backup manifest").read_text(encoding="utf-8")
        )
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BackupError("backup manifest is invalid") from exc


def _snapshot_member(snapshot: Path, relative: str) -> Path:
    candidate = snapshot / relative
    member = candidate.resolve()
    if snapshot not in member.parents or not member.is_file() or candidate.is_symlink():
        raise BackupError("backup manifest contains an unsafe path")
    return member


def _regular_file(path: str | Path, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file() or Path(path).is_symlink():
        raise BackupError(f"{label} is unavailable")
    return resolved


def _required_table(name: str) -> str:
    return next(item[3] for item in DATABASES if item[0] == name)


def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _release_compatible(
    snapshot: Mapping[str, Any], current: Mapping[str, str]
) -> bool:
    try:
        snapshot_major = str(snapshot["version"]).split(".", 1)[0]
        current_major = current["version"].split(".", 1)[0]
        return snapshot["id"] == current["id"] and snapshot_major == current_major
    except (KeyError, TypeError):
        return False


def _ownership(path: Path) -> dict[str, int]:
    status = path.stat()
    return {
        "mode": stat.S_IMODE(status.st_mode),
        "uid": getattr(status, "st_uid", 0),
        "gid": getattr(status, "st_gid", 0),
    }


def _apply_ownership(path: Path, ownership: Mapping[str, Any]) -> None:
    path.chmod(int(ownership["mode"]))
    chown = getattr(os, "chown", None)
    if chown is not None:
        chown(path, int(ownership["uid"]), int(ownership["gid"]))


def _verify_restored_ownership(target: Path, manifest: Mapping[str, Any]) -> None:
    for item in manifest["databases"]:
        if _ownership(target / item["path"]) != {
            key: int(item[key]) for key in ("mode", "uid", "gid")
        }:
            raise BackupError(
                f"restored ownership or permissions differ: {item['name']}"
            )
    for root_name, expected in manifest["roots"].items():
        if _ownership(target / "data" / root_name) != {
            key: int(expected[key]) for key in ("mode", "uid", "gid")
        }:
            raise BackupError(f"restored ownership or permissions differ: {root_name}")
    for key, filename in (
        ("configuration", "configuration.toml"),
        ("artifact_lock", "artifacts.lock.json"),
    ):
        expected = manifest["metadata"][key]
        if _ownership(target / "metadata" / filename) != {
            field: int(expected[field]) for field in ("mode", "uid", "gid")
        }:
            raise BackupError(f"restored ownership or permissions differ: {key}")


def _private_file(path: Path) -> None:
    path.chmod(0o600)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
                roots=roots,
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
