# ruff: noqa: F401, F811, I001, RUF100 -- split modules retain ticket context.
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


def test_restore_requires_a_private_administrative_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    snapshot = create_backup(
        destination=tmp_path / "backups",
        kind="nightly",
        configuration=configuration,
        artifact_lock=artifact_lock,
        roots=roots,
    )
    parent = (tmp_path / "restore-parent").resolve()
    parent.mkdir()
    parent.chmod(0o777)
    monkeypatch.setattr(administrative_backup.os, "geteuid", lambda: 0, raising=False)
    original_stat = Path.stat

    def root_owned_stat(path: Path, *args: object, **kwargs: object) -> object:
        result = original_stat(path, *args, **kwargs)
        if path == parent:
            return SimpleNamespace(st_uid=0, st_mode=result.st_mode)
        return result

    monkeypatch.setattr(Path, "stat", root_owned_stat)

    with pytest.raises(BackupError, match="restore parent"):
        restore_backup(
            snapshot=snapshot,
            target=parent / "restore",
            configuration=configuration,
            artifact_lock=artifact_lock,
        )


def test_backup_syncs_before_and_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    events: list[str] = []
    original_rename = Path.rename

    monkeypatch.setattr(
        administrative_backup,
        "_sync_tree",
        lambda _path: events.append("tree"),
    )
    monkeypatch.setattr(
        administrative_backup,
        "_sync_directory",
        lambda _path: events.append("directory"),
    )

    def recording_rename(source: Path, target: Path) -> Path:
        events.append("rename")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", recording_rename)
    create_backup(
        destination=tmp_path / "backups",
        kind="nightly",
        configuration=configuration,
        artifact_lock=artifact_lock,
        roots=roots,
    )

    assert events == ["tree", "rename", "directory"]


def test_bundle_ships_nightly_timer_and_reports_backup_freshness(
    tmp_path: Path,
) -> None:
    deployment = Path(__file__).parents[2] / "deployment"
    service = (deployment / "systemd" / "jarvis-backup.service").read_text(
        encoding="utf-8"
    )
    timer = (deployment / "systemd" / "jarvis-backup.timer").read_text(encoding="utf-8")
    assert "--kind nightly" in service
    assert "--image-digests /etc/jarvis/image-digests.json" in service
    assert "User=root" in service
    assert "/opt/jarvis/current/.venv/bin/python" in service
    assert "uv run" not in service
    assert "OnCalendar=" in timer
    assert "Persistent=true" in timer

    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    assert _backup_freshness(tmp_path, now=now) == "missing"
    snapshot = tmp_path / "20260812T110000.000000Z-nightly"
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
    partial = tmp_path / ".partial-interrupted"
    partial.mkdir()
    (partial / "manifest.json").write_text(
        json.dumps({"created_at": (now - timedelta(hours=1)).isoformat()}),
        encoding="utf-8",
    )
    assert _backup_freshness(tmp_path, now=now) == "stale"


def test_backup_rejects_image_digests_that_do_not_match_activated_containers(
    tmp_path: Path,
) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    (tmp_path / "image-digests.json").write_text(
        json.dumps({"broker": "jarvis-broker@sha256:" + "2" * 64}),
        encoding="utf-8",
    )

    def run(command: list[str], **_kwargs: object) -> object:
        if command[1] == "compose":
            return SimpleNamespace(
                stdout=json.dumps({"Service": "broker", "ID": "container-1"})
            )
        return SimpleNamespace(stdout="sha256:" + "1" * 64 + "\n")

    with pytest.raises(BackupError, match="activated images"):
        create_backup(
            destination=tmp_path / "backups",
            kind="nightly",
            configuration=configuration,
            artifact_lock=artifact_lock,
            roots=roots,
            runner=run,
        )


@pytest.mark.parametrize(
    ("output", "rows"),
    (
        (
            json.dumps(
                [
                    {"Service": "broker", "ID": "container-1"},
                    {"Service": "audit", "ID": "container-2"},
                ]
            ),
            ("broker", "audit"),
        ),
        (json.dumps({"Service": "broker", "ID": "container-1"}), ("broker",)),
        (
            "\n".join(
                json.dumps(row)
                for row in (
                    {"Service": "broker", "ID": "container-1"},
                    {"Service": "audit", "ID": "container-2"},
                )
            ),
            ("broker", "audit"),
        ),
    ),
    ids=("array", "single-object", "json-lines"),
)
def test_activated_images_accept_compose_json_shapes(
    output: str, rows: tuple[str, ...]
) -> None:

    def run(command: list[str], **_kwargs: object) -> object:
        if command[1] == "compose":
            return SimpleNamespace(stdout=output)
        return SimpleNamespace(
            stdout="".join(
                f"sha256:{index}" + str(index) * 63 + "\n"
                for index in range(1, len(rows) + 1)
            )
        )

    assert _activated_image_ids(Path("compose.yaml"), run) == {
        service: f"sha256:{index}" + str(index) * 63
        for index, service in enumerate(rows, start=1)
    }


@pytest.mark.parametrize(
    "output",
    (
        "not-json",
        json.dumps({"Service": "broker"}),
        "\n".join(
            json.dumps({"Service": "broker", "ID": container})
            for container in ("container-1", "container-2")
        ),
    ),
    ids=("malformed", "missing-id", "duplicate-service"),
)
def test_activated_images_reject_malformed_or_incomplete_rows(output: str) -> None:
    def run(command: list[str], **_kwargs: object) -> object:
        if command[1] == "compose":
            return SimpleNamespace(stdout=output)
        return SimpleNamespace(stdout="sha256:" + "1" * 64 + "\n")

    with pytest.raises(BackupError, match="activated images are unavailable"):
        _activated_image_ids(Path("compose.yaml"), run)


def test_backup_rejects_incomplete_compose_container_output(tmp_path: Path) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    (tmp_path / "compose.yaml").write_text(
        "name: jarvis-assistant-v1\nservices:\n"
        "  broker:\n    image: jarvis-broker\n"
        "  audit:\n    image: jarvis-audit\n",
        encoding="utf-8",
    )
    (tmp_path / "image-digests.json").write_text(
        json.dumps(
            {
                "broker": "jarvis-broker@sha256:" + "1" * 64,
                "audit": "jarvis-audit@sha256:" + "2" * 64,
            }
        ),
        encoding="utf-8",
    )

    def run(command: list[str], **_kwargs: object) -> object:
        if command[1] == "compose":
            return SimpleNamespace(
                stdout=json.dumps({"Service": "broker", "ID": "container-1"})
            )
        return SimpleNamespace(stdout="sha256:" + "1" * 64 + "\n")

    with pytest.raises(BackupError, match="image digests do not match"):
        create_backup(
            destination=tmp_path / "backups",
            kind="pre-change",
            configuration=configuration,
            artifact_lock=artifact_lock,
            roots=roots,
            runner=run,
        )
    assert not any((tmp_path / "backups").iterdir())
