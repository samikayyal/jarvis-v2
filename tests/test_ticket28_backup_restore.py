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
    (tmp_path / "compose.yaml").write_text(
        "name: jarvis-assistant-v1\nservices:\n  broker:\n    image: jarvis-broker\n",
        encoding="utf-8",
    )
    (tmp_path / "image-digests.json").write_text(
        json.dumps({"broker": "jarvis-broker@sha256:" + "1" * 64}),
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
    assert set(manifest["metadata"]) == {
        "configuration",
        "artifact_lock",
        "compose_manifest",
        "image_digests",
    }
    assert (snapshot / "metadata" / "compose.yaml").read_text(encoding="utf-8") == (
        tmp_path / "compose.yaml"
    ).read_text(encoding="utf-8")
    assert json.loads(
        (snapshot / "metadata" / "image-digests.json").read_text(encoding="utf-8")
    ) == {"broker": "jarvis-broker@sha256:" + "1" * 64}
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
    assert (restored / "metadata" / "compose.yaml").is_file()
    assert (restored / "metadata" / "image-digests.json").is_file()


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


def test_backup_manifest_uses_the_copied_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    original_copyfile = administrative_backup.shutil.copyfile

    def replace_lock_before_copy(source: str | Path, target: str | Path) -> str:
        if Path(source) == artifact_lock:
            artifact_lock.write_text(
                artifact_lock.read_text(encoding="utf-8").replace(
                    '"revision-28"', '"replacement-revision"'
                ),
                encoding="utf-8",
            )
        return original_copyfile(source, target)

    monkeypatch.setattr(
        administrative_backup.shutil, "copyfile", replace_lock_before_copy
    )
    snapshot = create_backup(
        destination=tmp_path / "backups",
        kind="nightly",
        configuration=configuration,
        artifact_lock=artifact_lock,
        roots=roots,
    )

    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["release"]["revision"] == "replacement-revision"


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


def test_backup_rejects_a_database_replaced_after_identity_capture(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _database(source, DATABASES["state"][1], "original")
    identity = administrative_backup._file_identity(source)
    _database(replacement, DATABASES["state"][1], "replacement")
    replacement.replace(source)

    with pytest.raises(BackupError, match="database changed during backup"):
        administrative_backup._sqlite_backup(
            source, tmp_path / "copied.sqlite3", identity
        )


def test_backup_releases_the_write_barrier_before_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    original_verify = administrative_backup._verify_database
    verified = False

    def verify_after_write(database: Path, required_table: str) -> str:
        nonlocal verified
        if not verified:
            with sqlite3.connect(
                roots["audit"] / "audit.sqlite3", timeout=0.01
            ) as writer:
                writer.execute(
                    "INSERT INTO audit_evidence VALUES ('verification-write')"
                )
            verified = True
        return original_verify(database, required_table)

    monkeypatch.setattr(administrative_backup, "_verify_database", verify_after_write)
    create_backup(
        destination=tmp_path / "backups",
        kind="nightly",
        configuration=configuration,
        artifact_lock=artifact_lock,
        roots=roots,
    )

    assert verified


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


def test_restore_verifies_the_files_copied_to_the_isolated_target(
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
    source = snapshot / "data" / "state" / "state.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _database(replacement, DATABASES["state"][1], "unverified-replacement")
    original_copyfile = administrative_backup.shutil.copyfile

    def replace_after_verification(copy_source: str | Path, target: str | Path) -> str:
        if Path(copy_source) == source and replacement.exists():
            replacement.replace(source)
        return original_copyfile(copy_source, target)

    monkeypatch.setattr(
        administrative_backup.shutil, "copyfile", replace_after_verification
    )
    target = tmp_path / "isolated-restore"
    with pytest.raises(BackupError, match="checksum"):
        restore_backup(
            snapshot=snapshot,
            target=target,
            configuration=configuration,
            artifact_lock=artifact_lock,
        )

    assert not target.exists()


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
    deployment = Path(__file__).parents[1] / "deployment"
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
    repository = Path(__file__).parents[1]
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
    repository = Path(__file__).parents[1]
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
    repository = Path(__file__).parents[1]
    bundle = tmp_path / "deployment"
    administrative_backup.shutil.copytree(repository / "deployment", bundle)
    lock_path = bundle / "artifacts.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["database_schemas"]["state"] = "0" * 64
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="schema fingerprints"):
        verify_bundle(bundle, source_root=repository)
