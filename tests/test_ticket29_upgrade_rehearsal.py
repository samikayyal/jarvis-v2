"""Ticket 29 pinned upgrade and rollback rehearsal acceptance seam."""

from __future__ import annotations

import gc
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_ticket28_backup_restore import _inputs

from jarvis_control_plane import (
    SQLiteDurableStateStore,
    upgrade_rehearsal,
)
from jarvis_control_plane.administrative_backup import create_backup
from jarvis_control_plane.sessions import SQLiteWorkingSessionStore
from jarvis_control_plane.upgrade_rehearsal import rehearse_upgrade

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)


def _schema_hash(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        schema = "\n".join(
            row[0] or ""
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                "ORDER BY type, name"
            )
        )
    return hashlib.sha256(schema.encode()).hexdigest()


@pytest.mark.parametrize("force_failure", [False, True])
def test_upgrade_reconciles_in_isolation_and_forced_failure_restores_previous_state(
    tmp_path: Path, force_failure: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, configuration, artifact_lock = _inputs(tmp_path)
    state_database = roots["state"] / "state.sqlite3"
    gc.collect()
    state_database.unlink()
    state = SQLiteDurableStateStore(state_database)
    state.close()
    session_database = roots["state"] / "sessions.sqlite3"
    gc.collect()
    session_database.unlink()
    sessions = SQLiteWorkingSessionStore(session_database)
    sessions.close()
    with sqlite3.connect(state_database) as connection:
        connection.execute(
            "INSERT INTO ingress_claims VALUES (?, ?, ?, ?, ?)",
            ("session", "inbound-1", "event-1", NOW.isoformat(), "dispatched"),
        )
        connection.execute(
            """
            INSERT INTO request_state(
                request_id, event_id, message_id, operator_id, session_id,
                chat_id, created_at, updated_at, status, phase, model, reasoning
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "request-active",
                "event-active",
                "inbound-1",
                "operator",
                "session",
                "chat",
                NOW.isoformat(),
                NOW.isoformat(),
                "processing",
                "orchestration",
                "gpt-5.6-terra",
                "medium",
            ),
        )
        for message_id, status, attempted_at in (
            ("reply-not-started", "unattempted", None),
            ("reply-unknown", "attempted", (NOW + timedelta(seconds=1)).isoformat()),
        ):
            connection.execute(
                "INSERT INTO outbound_attempt_record VALUES (?, ?, ?, ?, NULL, ?, ?, NULL)",
                (
                    "session",
                    message_id,
                    f"request-{message_id}",
                    status,
                    NOW.isoformat(),
                    attempted_at,
                ),
            )
            connection.execute(
                "INSERT INTO outbound_conversation_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "session",
                    message_id,
                    "working-session",
                    f"event-{message_id}",
                    "chat",
                    "operator",
                    "payload",
                    NOW.isoformat(),
                    f"request-{message_id}",
                    0,
                ),
            )
        connection.commit()
    lock = json.loads(artifact_lock.read_text(encoding="utf-8"))
    lock["database_schemas"]["state"] = _schema_hash(state_database)
    lock["database_schemas"]["sessions"] = _schema_hash(session_database)
    artifact_lock.write_text(json.dumps(lock), encoding="utf-8")
    snapshot = create_backup(
        destination=tmp_path / "backups",
        kind="pre-change",
        configuration=configuration,
        artifact_lock=artifact_lock,
        roots=roots,
        now=NOW,
    )
    active_sentinel = tmp_path / "active-service"
    active_sentinel.write_text("running", encoding="utf-8")
    verified: list[Path] = []
    for name in ("previous-release", "replacement-release"):
        bundle = tmp_path / name / "deployment"
        bundle.mkdir(parents=True)
        (bundle / "artifacts.lock.json").write_bytes(artifact_lock.read_bytes())
    monkeypatch.setattr(
        upgrade_rehearsal,
        "__file__",
        str(
            tmp_path
            / "replacement-release"
            / "src"
            / "jarvis_control_plane"
            / "upgrade_rehearsal.py"
        ),
    )

    def verify_release(_bundle: Path, **kwargs: Path) -> None:
        assert kwargs["configuration"] == configuration.resolve()
        verified.append(kwargs["source_root"])

    monkeypatch.setattr(upgrade_rehearsal, "verify_bundle", verify_release)

    report = rehearse_upgrade(
        previous_release=tmp_path / "previous-release",
        replacement_release=tmp_path / "replacement-release",
        configuration=configuration,
        snapshot=snapshot,
        workspace=tmp_path / "rehearsal",
        admission_stopped_at=NOW - timedelta(seconds=1),
        window_start=NOW - timedelta(minutes=1),
        window_end=NOW + timedelta(minutes=1),
        force_failure=force_failure,
    )

    assert report.outcome == ("rolled_back" if force_failure else "rehearsed")
    assert report.admission_stopped is True
    assert report.ingress_claims == 1
    assert report.requests_interrupted == 1
    assert report.outbound_not_started == 1
    assert report.outbound_unknown == 1
    assert verified == [
        (tmp_path / "previous-release").resolve(),
        (tmp_path / "replacement-release").resolve(),
    ]
    with sqlite3.connect(report.candidate_state) as connection:
        assert connection.execute(
            "SELECT message_id, status FROM outbound_attempt_record ORDER BY message_id"
        ).fetchall() == [
            ("reply-not-started", "not_started"),
            ("reply-unknown", "unknown"),
        ]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM outbound_conversation_outbox"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute(
            "SELECT status, phase, outcome FROM request_state "
            "WHERE request_id = 'request-active'"
        ).fetchone() == ("interrupted", "interrupted", "interrupted")
    if force_failure:
        assert report.rollback_state is not None
        with sqlite3.connect(report.rollback_state) as connection:
            assert connection.execute(
                "SELECT message_id, status FROM outbound_attempt_record "
                "ORDER BY message_id"
            ).fetchall() == [
                ("reply-not-started", "unattempted"),
                ("reply-unknown", "attempted"),
            ]
            assert (
                connection.execute(
                    "SELECT status FROM request_state "
                    "WHERE request_id = 'request-active'"
                ).fetchone()[0]
                == "processing"
            )
    else:
        assert report.rollback_state is None
    assert active_sentinel.read_text(encoding="utf-8") == "running"
