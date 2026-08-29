# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis_control_plane import administrative_backup, deployment
from jarvis_control_plane.administrative_backup import (
    BackupError,
    _activated_image_ids,
    _consistent_snapshot,
    create_backup,
    restore_backup,
)
from jarvis_control_plane.deployment import (
    BundleValidationError,
    _backup_freshness,
    verify_bundle,
)

DATABASES = {
    "state": ("state.sqlite3", "CREATE TABLE request_state (request_id TEXT)"),
    "sessions": (
        "sessions.sqlite3",
        "CREATE TABLE working_session_current (slot INTEGER)",
    ),
    "audit": ("audit.sqlite3", "CREATE TABLE audit_evidence (evidence_id TEXT)"),
    "traces": (
        "traces.sqlite3",
        "CREATE TABLE diagnostic_traces (trace_id TEXT)",
    ),
    "google_traces": (
        "google.sqlite3",
        "CREATE TABLE diagnostic_traces (trace_id TEXT)",
    ),
    "deleted_conversations": (
        "deleted-conversations.sqlite3",
        "CREATE TABLE deleted_messages (message_id TEXT)",
    ),
}


def _database(path: Path, schema: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(schema)
        table = schema.split("TABLE ", 1)[1].split(" ", 1)[0]
        column = connection.execute(f"PRAGMA table_info({table})").fetchone()[1]
        connection.execute(f"INSERT INTO {table} ({column}) VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


def _inputs(tmp_path: Path) -> tuple[dict[str, Path], Path, Path]:
    roots: dict[str, Path] = {}
    for name, (filename, schema) in DATABASES.items():
        root_name = "state" if name == "sessions" else name
        root = roots.setdefault(root_name, tmp_path / "live" / root_name)
        _database(root / filename, schema, name)

    configuration = tmp_path / "jarvis.toml"
    shipped = Path(__file__).parents[2] / "deployment"
    configuration.write_bytes((shipped / "config.example.toml").read_bytes())
    artifact_lock = tmp_path / "artifacts.lock.json"
    schemas = {}
    for name, (filename, _schema) in DATABASES.items():
        root_name = "state" if name == "sessions" else name
        with sqlite3.connect(roots[root_name] / filename) as connection:
            schema_text = "\n".join(
                row[0] or ""
                for row in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
                )
            )
        schemas[name] = hashlib.sha256(schema_text.encode()).hexdigest()
    artifact_lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "application": {
                    "name": "jarvis-v2",
                    "version": "0.1.0",
                    "git_revision": "revision-28",
                },
                "database_schemas": schemas,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "compose.yaml").write_text(
        "name: jarvis-assistant-v1\nservices:\n  broker:\n    image: jarvis-broker\n",
        encoding="utf-8",
    )
    (tmp_path / "image-digests.json").write_text(
        json.dumps({"broker": "jarvis-broker@sha256:" + "1" * 64}),
        encoding="utf-8",
    )
    return roots, configuration, artifact_lock


def test_backup_rejects_an_audit_row_the_safe_reader_cannot_parse(
    tmp_path: Path,
) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    audit_database = roots["audit"] / "audit.sqlite3"
    with sqlite3.connect(audit_database) as connection:
        connection.execute("DROP TABLE audit_evidence")
    from jarvis_control_plane.adapters import SQLiteAuditBoundary

    SQLiteAuditBoundary(audit_database).close()
    with sqlite3.connect(audit_database) as connection:
        connection.execute(
            """
            INSERT INTO audit_evidence(
                evidence_id, kind, occurred_at, outcome, actor, details_json, redacted
            ) VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            ("bad-audit", "test", "not-a-date", "ok", "test", "{}"),
        )
    with sqlite3.connect(audit_database) as connection:
        schema = "\n".join(
            row[0] or ""
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
            )
        )
    lock = json.loads(artifact_lock.read_text(encoding="utf-8"))
    lock["database_schemas"]["audit"] = hashlib.sha256(schema.encode()).hexdigest()
    artifact_lock.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(BackupError, match="audit readability"):
        create_backup(
            destination=tmp_path / "backups",
            kind="nightly",
            configuration=configuration,
            artifact_lock=artifact_lock,
            roots=roots,
        )


def test_administrative_status_cli_accepts_a_custom_backup_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(deployment, "verify_bundle", lambda *_args, **_kwargs: None)

    def status(_bundle: Path, **kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {}

    monkeypatch.setattr(deployment, "administrative_status", status)
    backup_root = tmp_path / "backups"
    activation_override = tmp_path / "activation.compose.yaml"
    activation_override.write_text("services: {}\n", encoding="utf-8")

    assert (
        deployment.main(
            [
                str(tmp_path),
                "--administrative-status",
                "--activation-override",
                str(activation_override),
                "--backup-root",
                str(backup_root),
            ]
        )
        == 0
    )
    assert observed == {
        "activation_override": activation_override,
        "backup_root": backup_root,
    }


def test_bundle_rejects_effective_backup_unit_overrides(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    bundle = tmp_path / "deployment"
    administrative_backup.shutil.copytree(repository / "deployment", bundle)
    service = bundle / "systemd" / "jarvis-backup.service"
    service.write_text(
        service.read_text(encoding="utf-8")
        + "\nUser=nobody\nExecStart=\nExecStart=/bin/true\n",
        encoding="utf-8",
    )

    with pytest.raises(BundleValidationError, match="nightly backup service"):
        verify_bundle(bundle, source_root=repository)


def test_bundle_rejects_extra_backup_unit_directives(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    bundle = tmp_path / "deployment"
    administrative_backup.shutil.copytree(repository / "deployment", bundle)
    service = bundle / "systemd" / "jarvis-backup.service"
    service.write_text(
        service.read_text(encoding="utf-8") + "\nExecStartPost=/bin/true\n",
        encoding="utf-8",
    )

    with pytest.raises(BundleValidationError, match="nightly backup service"):
        verify_bundle(bundle, source_root=repository)


def test_backup_rejects_a_destination_not_owned_by_the_effective_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    destination = (tmp_path / "backups").resolve()
    destination.mkdir()
    original_stat = Path.stat

    def stat_with_other_owner(path: Path, *args: object, **kwargs: object) -> object:
        result = original_stat(path, *args, **kwargs)
        if path == destination:
            return SimpleNamespace(st_uid=12345, st_mode=result.st_mode)
        return result

    monkeypatch.setattr(Path, "stat", stat_with_other_owner)
    monkeypatch.setattr(administrative_backup.os, "geteuid", lambda: 0, raising=False)

    with pytest.raises(BackupError, match="owner and mode"):
        create_backup(
            destination=destination,
            kind="nightly",
            configuration=configuration,
            artifact_lock=artifact_lock,
            roots=roots,
        )


def test_bundle_rejects_unreviewed_database_schema_fingerprints(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    bundle = tmp_path / "deployment"
    administrative_backup.shutil.copytree(repository / "deployment", bundle)
    lock_path = bundle / "artifacts.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["database_schemas"]["state"] = "0" * 64
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="schema fingerprints"):
        verify_bundle(bundle, source_root=repository)
