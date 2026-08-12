"""Manual administrative backup and isolated restore tooling for Jarvis."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

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
PROTECTED_ADMIN_ROOTS = tuple(DEFAULT_ROOTS.values()) + (
    Path("/var/lib/jarvis/vault"),
    Path("/srv/jarvis-workspace"),
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
        schemas = lock.get("database_schemas")
        if not isinstance(schemas, dict) or set(schemas) != {
            item[0] for item in DATABASES
        }:
            raise TypeError
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in schemas.values()
        ):
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


def _sqlite_backup(source: Path, target: Path, identity: tuple[int, int]) -> None:
    if _file_identity(source) != identity:
        raise BackupError(f"database changed during backup: {source.name}")
    uri = f"file:{quote(source.as_posix(), safe='/:')}?mode=ro"
    source_connection = sqlite3.connect(uri, uri=True)
    target_connection = sqlite3.connect(target)
    try:
        if _file_identity(source) != identity:
            raise BackupError(f"database changed during backup: {source.name}")
        source_connection.backup(target_connection)
        if _file_identity(source) != identity:
            raise BackupError(f"database changed during backup: {source.name}")
        target_connection.execute("PRAGMA journal_mode = DELETE")
    finally:
        target_connection.close()
        source_connection.close()


@contextmanager
def _consistent_snapshot(
    databases: Sequence[Path],
) -> Iterator[list[tuple[int, int]]]:
    """Hold one SQLite write barrier across every authoritative database."""

    connection = sqlite3.connect("file::memory:?cache=private", uri=True, timeout=30)
    try:
        identities = [_file_identity(database) for database in databases]
        for index, (database, identity) in enumerate(zip(databases, identities)):
            uri = f"file:{quote(database.as_posix(), safe='/:')}"
            connection.execute(f'ATTACH DATABASE ? AS "backup_{index}"', (uri,))
            if _file_identity(database) != identity:
                raise BackupError(f"database changed during backup: {database.name}")
        connection.execute("BEGIN IMMEDIATE")
        yield identities
    except sqlite3.Error as exc:
        raise BackupError("consistent database snapshot could not be acquired") from exc
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _file_identity(path: Path) -> tuple[int, int]:
    status = path.stat()
    return status.st_dev, status.st_ino


def _verify_database(database: Path, required_table: str) -> str:
    uri = f"file:{quote(database.as_posix(), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise BackupError(f"database integrity check failed: {database.name}")
        connection.execute(f'SELECT COUNT(*) FROM "{required_table}"').fetchone()
        if required_table == "audit_evidence":
            _verify_audit_records(connection)
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


def _verify_audit_records(connection: sqlite3.Connection) -> None:
    from .adapters import SQLiteAuditBoundary

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(audit_evidence)")
    }
    if not set(SQLiteAuditBoundary._AUDIT_COLUMNS) <= columns:
        return
    connection.row_factory = sqlite3.Row
    try:
        for row in connection.execute("SELECT * FROM audit_evidence"):
            SQLiteAuditBoundary._evidence_from_row(row)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BackupError("audit readability check failed") from exc


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
    _validate_deployment_metadata(
        _snapshot_member(snapshot, manifest["metadata"]["compose_manifest"]["path"]),
        _snapshot_member(snapshot, manifest["metadata"]["image_digests"]["path"]),
    )


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
        name for name, _filename in METADATA_FILES
    }:
        raise BackupError("backup manifest is incomplete")
    for item in metadata.values():
        _validate_file_record(item)
    if any(
        metadata[name]["path"] != f"metadata/{filename}"
        for name, filename in METADATA_FILES
    ):
        raise BackupError("backup metadata inventory is invalid")


def _validate_deployment_metadata(
    compose_path: Path,
    digests_path: Path,
    *,
    activated_image_ids: Mapping[str, str] | None = None,
) -> None:
    try:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        services = compose.get("services") if isinstance(compose, dict) else None
        digests = json.loads(digests_path.read_text(encoding="utf-8"))
        if (
            not isinstance(services, dict)
            or not services
            or not isinstance(digests, dict)
            or set(digests) != set(services)
            or any(
                not isinstance(name, str)
                or not isinstance(value, str)
                or re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", value) is None
                for name, value in digests.items()
            )
        ):
            raise TypeError
        if activated_image_ids is not None and {
            name: value.rsplit("@", 1)[1] for name, value in digests.items()
        } != dict(activated_image_ids):
            raise BackupError("image digests do not match the activated images")
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        yaml.YAMLError,
    ) as exc:
        raise BackupError("deployment metadata is invalid") from exc


def _activated_image_ids(
    compose_path: Path, runner: Callable[..., object]
) -> dict[str, str]:
    try:
        base = [
            "docker",
            "compose",
            "--file",
            str(compose_path),
            "--profile",
            "manual-activation",
        ]
        observed = runner(
            [*base, "ps", "--all", "--format", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
        rows = json.loads(str(getattr(observed, "stdout", "")) or "[]")
        if isinstance(rows, Mapping):
            rows = [rows]
        containers = {
            row["Service"]: row["ID"]
            for row in rows
            if isinstance(row, Mapping) and row.get("Service") and row.get("ID")
        }
        if len(containers) != len(rows) or not containers:
            raise TypeError
        inspected = runner(
            ["docker", "inspect", "--format", "{{.Image}}", *containers.values()],
            check=True,
            capture_output=True,
            text=True,
        )
        image_ids = str(getattr(inspected, "stdout", "")).splitlines()
        if len(image_ids) != len(containers):
            raise TypeError
        return dict(zip(containers, image_ids))
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        raise BackupError("activated images are unavailable") from exc


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


def _ownership(path: Path, identity: tuple[int, int] | None = None) -> dict[str, int]:
    status = path.stat()
    if identity is not None and (status.st_dev, status.st_ino) != identity:
        raise BackupError(f"database changed during backup: {path.name}")
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
    for key, filename in METADATA_FILES:
        expected = manifest["metadata"][key]
        if _ownership(target / "metadata" / filename) != {
            field: int(expected[field]) for field in ("mode", "uid", "gid")
        }:
            raise BackupError(f"restored ownership or permissions differ: {key}")


def _private_file(path: Path) -> None:
    path.chmod(0o600)


def _trusted_admin_directory(path: Path, label: str) -> None:
    if not path.is_dir() or path.is_symlink():
        raise BackupError(f"{label} must be an existing private directory")
    get_effective_uid = getattr(os, "geteuid", None)
    if get_effective_uid is None:
        return
    directory_stat = path.stat()
    if directory_stat.st_uid != get_effective_uid() or directory_stat.st_mode & 0o022:
        raise BackupError(f"{label} must have the effective owner and private mode")


def _sync_tree(root: Path) -> None:
    if os.name == "nt":
        return
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        _sync_directory(path)
    _sync_directory(root)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
