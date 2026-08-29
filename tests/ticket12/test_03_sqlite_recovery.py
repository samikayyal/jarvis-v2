"""Ticket 12 SQLite recovery tests."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from jarvis_control_plane import (
    InboundMessage,
    OutboundAttemptStatus,
    SignedInboundEvent,
    SQLiteDurableStateStore,
    StateStoreError,
    migrate_sqlite_outbound_conversation_attempts,
)

from .helpers import (
    NOW,
    OPERATOR,
    TRANSPORT_SESSION,
    _components,
    _outbound_message,
    _restart_inconsistency_reasons,
    _sqlite_reserved_message,
)


@pytest.mark.parametrize("legacy_attempt_schema", [False, True])
def test_pre_ticket12_sqlite_outbox_requires_manual_migration_without_mutation(
    legacy_attempt_schema: bool,
    tmp_path,
) -> None:
    database = tmp_path / "legacy-ticket20-state.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE outbound_conversation_outbox (
            transport_session_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            working_session_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            text TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            request_id TEXT NOT NULL,
            credential_like INTEGER NOT NULL CHECK (credential_like IN (0, 1)),
            PRIMARY KEY (transport_session_id, message_id)
        );
        """
    )
    if legacy_attempt_schema:
        connection.executescript(
            """
            CREATE TABLE outbound_attempt_record (
                transport_session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('unattempted', 'attempted', 'confirmed', 'unknown', 'not_started')
                ),
                reserved_at TEXT NOT NULL,
                attempted_at TEXT,
                terminal_at TEXT,
                PRIMARY KEY (transport_session_id, message_id)
            );
            """
        )
    connection.execute(
        """
        INSERT INTO outbound_conversation_outbox(
            transport_session_id, message_id, working_session_id, event_id,
            chat_id, sender_id, text, occurred_at, request_id, credential_like
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            TRANSPORT_SESSION,
            "legacy-reply",
            "legacy-working-session",
            "legacy-event",
            OPERATOR,
            "jarvis",
            "legacy private outbound body",
            NOW.isoformat(),
            "legacy-request",
            0,
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(StateStoreError, match="manual Ticket 12 migration"):
        SQLiteDurableStateStore(database)

    unchanged = sqlite3.connect(database)
    attempt_table = unchanged.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = 'outbound_attempt_record'
        """
    ).fetchone()
    if legacy_attempt_schema:
        assert attempt_table is not None
        assert "outbound_id" not in {
            row[1]
            for row in unchanged.execute(
                "PRAGMA table_info(outbound_attempt_record)"
            ).fetchall()
        }
    else:
        assert attempt_table is None
    assert (
        unchanged.execute(
            "SELECT text FROM outbound_conversation_outbox WHERE message_id = ?",
            ("legacy-reply",),
        ).fetchone()[0]
        == "legacy private outbound body"
    )
    unchanged.close()

    assert migrate_sqlite_outbound_conversation_attempts(database, applied_at=NOW) == 1
    assert migrate_sqlite_outbound_conversation_attempts(database, applied_at=NOW) == 1

    state = SQLiteDurableStateStore(database)
    restarted = _components(state=state)

    attempts = state.list_outbound_conversation_attempts()
    assert len(attempts) == 1
    assert attempts[0].status is OutboundAttemptStatus.UNKNOWN
    assert attempts[0].message is None
    assert restarted.outbound.sent == []
    state.close()


def test_sqlite_restart_missing_open_outbox_is_audited_and_degraded(tmp_path) -> None:
    database, message = _sqlite_reserved_message(tmp_path, "missing-open-outbox")
    connection = sqlite3.connect(database)
    connection.execute(
        "DELETE FROM outbound_conversation_outbox WHERE message_id = ?",
        (message.message_id,),
    )
    connection.commit()
    connection.close()

    state = SQLiteDurableStateStore(database)
    restarted = _components(state=state)

    attempt = state.list_outbound_conversation_attempts()[0]
    assert attempt.status is OutboundAttemptStatus.NOT_STARTED
    assert attempt.message is None
    assert restarted.broker.recovery_degraded is True
    assert restarted.broker.recovery_degraded_reason is not None
    assert _restart_inconsistency_reasons(restarted) == {"open_attempt_without_outbox"}
    result = restarted.receiver.receive(
        SignedInboundEvent.from_message(
            InboundMessage(
                event_type="message.received",
                session_id=TRANSPORT_SESSION,
                event_id="event-recovery-degraded",
                message_id="message-recovery-degraded",
                sender_id=OPERATOR,
                chat_id=OPERATOR,
                chat_type="direct",
                message_type="text",
                from_me=False,
                text="do not start work while degraded",
            ),
            restarted.config.signing_secret,
        )
    )
    assert result.status_code == 503
    assert result.disposition == "recovery_degraded"
    assert state.list_requests() == ()
    state.close()


def test_sqlite_recovery_degraded_marker_survives_two_restarts_until_ack(
    tmp_path,
) -> None:
    database, message = _sqlite_reserved_message(
        tmp_path, "persistent-recovery-degraded"
    )
    connection = sqlite3.connect(database)
    connection.execute(
        "DELETE FROM outbound_conversation_outbox WHERE message_id = ?",
        (message.message_id,),
    )
    connection.commit()
    connection.close()

    first_state = SQLiteDurableStateStore(database)
    first = _components(state=first_state)
    first_marker = first_state.load_recovery_degraded_marker()
    assert first.broker.recovery_degraded is True
    assert first_marker is not None
    first_state.close()

    second_state = SQLiteDurableStateStore(database)
    second = _components(state=second_state)
    assert second.broker.recovery_degraded is True
    assert second.broker.recovery_degraded_reason == first_marker.reason
    result = second.receiver.receive(
        SignedInboundEvent.from_message(
            InboundMessage(
                event_type="message.received",
                session_id=TRANSPORT_SESSION,
                event_id="event-persistent-recovery-degraded",
                message_id="message-persistent-recovery-degraded",
                sender_id=OPERATOR,
                chat_id=OPERATOR,
                chat_type="direct",
                message_type="text",
                from_me=False,
                text="do not start work while the marker remains",
            ),
            second.config.signing_secret,
        )
    )
    assert result.status_code == 503
    assert result.disposition == "recovery_degraded"
    assert second_state.load_recovery_degraded_marker() is not None

    second_state.acknowledge_recovery_degraded()
    assert second_state.load_recovery_degraded_marker() is None
    second_state.close()

    third_state = SQLiteDurableStateStore(database)
    third = _components(state=third_state)
    assert third.broker.recovery_degraded is False
    third_state.close()


def test_sqlite_restart_orphan_outbox_is_removed_and_degraded(tmp_path) -> None:
    database, message = _sqlite_reserved_message(tmp_path, "orphan-outbox")
    connection = sqlite3.connect(database)
    connection.execute(
        "DELETE FROM outbound_attempt_record WHERE message_id = ?",
        (message.message_id,),
    )
    connection.commit()
    connection.close()

    state = SQLiteDurableStateStore(database)
    restarted = _components(state=state)

    assert state.list_outbound_conversation_attempts() == ()
    assert restarted.broker.recovery_degraded is True
    assert _restart_inconsistency_reasons(restarted) == {"outbox_without_attempt"}
    state.close()


def test_sqlite_restart_terminal_attempt_with_payload_is_removed_and_degraded(
    tmp_path,
) -> None:
    database = tmp_path / "terminal-outbox.sqlite3"
    state = SQLiteDurableStateStore(database)
    message = _outbound_message(message_id="terminal-with-payload")
    state.reserve_outbound_conversation_message(message)
    state.mark_outbound_conversation_attempted(
        transport_session_id=message.transport_session_id,
        message_id=message.message_id,
        attempted_at=NOW + timedelta(seconds=1),
    )
    state.terminalize_outbound_conversation_attempt(
        transport_session_id=message.transport_session_id,
        message_id=message.message_id,
        status=OutboundAttemptStatus.CONFIRMED,
        terminal_at=NOW + timedelta(seconds=2),
        outbound_id="gateway-terminal-with-payload",
    )
    state.close()

    connection = sqlite3.connect(database)
    connection.execute(
        """
        INSERT INTO outbound_conversation_outbox(
            transport_session_id, message_id, working_session_id, event_id,
            chat_id, sender_id, text, occurred_at, request_id, credential_like
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message.transport_session_id,
            message.message_id,
            message.working_session_id,
            message.event_id,
            message.chat_id,
            message.sender_id,
            message.text,
            message.occurred_at.isoformat(),
            message.request_id,
            int(message.credential_like),
        ),
    )
    connection.commit()
    connection.close()

    reopened = SQLiteDurableStateStore(database)
    restarted = _components(state=reopened)

    attempt = reopened.list_outbound_conversation_attempts()[0]
    assert attempt.status is OutboundAttemptStatus.CONFIRMED
    assert attempt.message is None
    assert restarted.broker.recovery_degraded is True
    assert _restart_inconsistency_reasons(restarted) == {"terminal_attempt_with_outbox"}
    reopened.close()


def test_sqlite_restart_mismatched_attempt_outbox_request_is_degraded(tmp_path) -> None:
    database, message = _sqlite_reserved_message(tmp_path, "mismatched-request")
    connection = sqlite3.connect(database)
    connection.execute(
        """
        UPDATE outbound_conversation_outbox
        SET request_id = ?
        WHERE transport_session_id = ? AND message_id = ?
        """,
        ("different-request", message.transport_session_id, message.message_id),
    )
    connection.commit()
    connection.close()

    state = SQLiteDurableStateStore(database)
    restarted = _components(state=state)

    attempt = state.list_outbound_conversation_attempts()[0]
    assert attempt.status is OutboundAttemptStatus.NOT_STARTED
    assert attempt.message is None
    assert restarted.broker.recovery_degraded is True
    assert _restart_inconsistency_reasons(restarted) == {
        "attempt_outbox_request_mismatch"
    }
    state.close()
