from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis_control_plane.administrative_backup import (
    BackupError,
    _consistent_snapshot,
    create_backup,
    restore_backup,
)
from jarvis_control_plane.deployment import _backup_freshness

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
    "codex_traces": (
        "codex.sqlite3",
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
    shipped = Path(__file__).parents[1] / "deployment"
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
    return roots, configuration, artifact_lock


def test_nightly_backup_and_isolated_restore_cover_every_authoritative_store(
    tmp_path: Path,
) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    (roots["state"] / "cache.sqlite3").write_bytes(b"excluded cache")
    credentials = tmp_path / "live" / "credentials" / "token.json"
    credentials.parent.mkdir()
    credentials.write_text("credential-must-not-be-backed-up", encoding="utf-8")
    openwa = tmp_path / "live" / "openwa" / "pairing.json"
    openwa.parent.mkdir()
    openwa.write_text("pairing-must-not-be-backed-up", encoding="utf-8")

    snapshot = create_backup(
        destination=tmp_path / "backups",
        kind="nightly",
        configuration=configuration,
        artifact_lock=artifact_lock,
        roots=roots,
    )

    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "nightly"
    assert manifest["schema_version"] == 1
    assert manifest["release"] == {
        "id": "jarvis-assistant-v1",
        "version": "0.1.0",
        "revision": "revision-28",
    }
    assert {item["name"] for item in manifest["databases"]} == set(DATABASES)
    snapshot_text = " ".join(path.name for path in snapshot.rglob("*"))
    assert "cache.sqlite3" not in snapshot_text
    snapshot_bytes = b"".join(
        path.read_bytes() for path in snapshot.rglob("*") if path.is_file()
    )
    assert b"credential-must-not-be-backed-up" not in snapshot_bytes
    assert "pairing.json" not in snapshot_text

    restored = restore_backup(
        snapshot=snapshot,
        target=tmp_path / "isolated-restore",
        configuration=configuration,
        artifact_lock=artifact_lock,
    )

    for name, (filename, schema) in DATABASES.items():
        root_name = "state" if name == "sessions" else name
        database = restored / "data" / root_name / filename
        table = schema.split("TABLE ", 1)[1].split(" ", 1)[0]
        with sqlite3.connect(database) as connection:
            assert connection.execute(f"SELECT * FROM {table}").fetchone()[0] == name
        if os.name == "posix":
            assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_pre_change_backup_uses_sqlite_online_snapshot_for_wal_data(
    tmp_path: Path,
) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    live = sqlite3.connect(roots["state"] / "state.sqlite3")
    live.execute("PRAGMA journal_mode = WAL")
    live.execute("INSERT INTO request_state VALUES ('committed-in-wal')")
    live.commit()
    try:
        snapshot = create_backup(
            destination=tmp_path / "backups",
            kind="pre-change",
            configuration=configuration,
            artifact_lock=artifact_lock,
            roots=roots,
        )
    finally:
        live.close()

    copied = snapshot / "data" / "state" / "state.sqlite3"
    assert not (snapshot / "data" / "state" / "state.sqlite3-wal").exists()
    with sqlite3.connect(copied) as connection:
        values = [row[0] for row in connection.execute("SELECT * FROM request_state")]
    assert values == ["state", "committed-in-wal"]


def test_consistent_snapshot_holds_one_write_barrier_across_all_stores(
    tmp_path: Path,
) -> None:
    roots, _configuration, _artifact_lock = _inputs(tmp_path)
    databases = [
        roots["state"] / "state.sqlite3",
        roots["audit"] / "audit.sqlite3",
    ]

    with _consistent_snapshot(databases):
        writer = sqlite3.connect(databases[1], timeout=0.01)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("INSERT INTO audit_evidence VALUES ('blocked')")
        finally:
            writer.close()


def test_restore_rejects_tampering_and_incompatible_release(tmp_path: Path) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    snapshot = create_backup(
        destination=tmp_path / "backups",
        kind="nightly",
        configuration=configuration,
        artifact_lock=artifact_lock,
        roots=roots,
    )
    copied = snapshot / "data" / "state" / "state.sqlite3"
    copied.write_bytes(copied.read_bytes() + b"tampered")

    with pytest.raises(BackupError, match="checksum"):
        restore_backup(
            snapshot=snapshot,
            target=tmp_path / "tampered-restore",
            configuration=configuration,
            artifact_lock=artifact_lock,
        )
    assert not (tmp_path / "tampered-restore").exists()

    clean = create_backup(
        destination=tmp_path / "backups-2",
        kind="nightly",
        configuration=configuration,
        artifact_lock=artifact_lock,
        roots=roots,
    )
    compatible = tmp_path / "compatible-lock.json"
    compatible.write_text(
        artifact_lock.read_text(encoding="utf-8").replace(
            '"revision-28"', '"compatible-revision"'
        ),
        encoding="utf-8",
    )
    restore_backup(
        snapshot=clean,
        target=tmp_path / "compatible-restore",
        configuration=configuration,
        artifact_lock=compatible,
    )
    incompatible = tmp_path / "incompatible-lock.json"
    incompatible.write_text(
        artifact_lock.read_text(encoding="utf-8").replace(
            '"version": "0.1.0"', '"version": "2.0.0"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(BackupError, match="release compatibility"):
        restore_backup(
            snapshot=clean,
            target=tmp_path / "incompatible-restore",
            configuration=configuration,
            artifact_lock=incompatible,
        )

    incompatible_schema = tmp_path / "incompatible-schema-lock.json"
    incompatible_schema.write_text(
        artifact_lock.read_text(encoding="utf-8").replace(
            json.loads(artifact_lock.read_text(encoding="utf-8"))["database_schemas"][
                "state"
            ],
            "0" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(BackupError, match="database schema compatibility"):
        restore_backup(
            snapshot=clean,
            target=tmp_path / "incompatible-schema-restore",
            configuration=configuration,
            artifact_lock=incompatible_schema,
        )


def test_restore_requires_a_new_isolated_target(tmp_path: Path) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    snapshot = create_backup(
        destination=tmp_path / "backups",
        kind="nightly",
        configuration=configuration,
        artifact_lock=artifact_lock,
        roots=roots,
    )
    target = tmp_path / "already-present"
    target.mkdir()

    with pytest.raises(BackupError, match="must not already exist"):
        restore_backup(
            snapshot=snapshot,
            target=target,
            configuration=configuration,
            artifact_lock=artifact_lock,
        )
    with pytest.raises(BackupError, match="outside the snapshot"):
        restore_backup(
            snapshot=snapshot,
            target=snapshot / "nested-restore",
            configuration=configuration,
            artifact_lock=artifact_lock,
        )
    with pytest.raises(BackupError, match="outside Jarvis data roots"):
        create_backup(
            destination=roots["state"] / "backups",
            kind="nightly",
            configuration=configuration,
            artifact_lock=artifact_lock,
            roots=roots,
        )


def test_bundle_ships_nightly_timer_and_reports_backup_freshness(
    tmp_path: Path,
) -> None:
    deployment = Path(__file__).parents[1] / "deployment"
    service = (deployment / "systemd" / "jarvis-backup.service").read_text(
        encoding="utf-8"
    )
    timer = (deployment / "systemd" / "jarvis-backup.timer").read_text(encoding="utf-8")
    assert "--kind nightly" in service
    assert "User=root" in service
    assert "OnCalendar=" in timer
    assert "Persistent=true" in timer

    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    assert _backup_freshness(tmp_path, now=now) == "missing"
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(
        json.dumps({"created_at": (now - timedelta(hours=1)).isoformat()}),
        encoding="utf-8",
    )
    assert _backup_freshness(tmp_path, now=now) == "current"
    (snapshot / "manifest.json").write_text(
        json.dumps({"created_at": (now - timedelta(hours=48)).isoformat()}),
        encoding="utf-8",
    )
    assert _backup_freshness(tmp_path, now=now) == "stale"
