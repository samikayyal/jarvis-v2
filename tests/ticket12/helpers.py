"""Shared Ticket 12 test helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from test_support import build_receiver_components

from jarvis_control_plane import (
    ConversationMessage,
    InMemoryDurableStateStore,
    RequestState,
    SQLiteDurableStateStore,
)

NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
OPERATOR = "operator-001"
TRANSPORT_SESSION = "openwa-internal-session"


def _components(
    *,
    state: object,
    audit: object | None = None,
    working_sessions: object | None = None,
):
    return build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket12-recovery-secret",
        now=NOW,
        id_prefix="ticket12",
        state=state,
        audit=audit,
        working_sessions=working_sessions,
    )


def _request(
    request_id: str,
    *,
    status: str,
    phase: str,
    outcome: str | None = None,
) -> RequestState:
    return RequestState(
        request_id=request_id,
        event_id=f"event-{request_id}",
        message_id=f"message-{request_id}",
        operator_id=OPERATOR,
        session_id=TRANSPORT_SESSION,
        chat_id=OPERATOR,
        created_at=NOW,
        updated_at=NOW,
        status=status,
        phase=phase,
        outcome=outcome,
    )


def _outbound_message(*, message_id: str) -> ConversationMessage:
    return ConversationMessage(
        working_session_id="working-session",
        transport_session_id=TRANSPORT_SESSION,
        message_id=message_id,
        event_id=f"event-{message_id}",
        chat_id=OPERATOR,
        sender_id="jarvis",
        text="bounded reply body",
        occurred_at=NOW,
        direction="outbound",
        request_id=f"request-{message_id}",
    )


def _restart_inconsistency_reasons(components: object) -> set[str]:
    return {
        record.details["reason"]
        for record in components.audit.records
        if record.kind == "restart_inconsistency"
    }


def _sqlite_reserved_message(tmp_path, message_id: str):
    database = tmp_path / f"{message_id}.sqlite3"
    state = SQLiteDurableStateStore(database)
    message = _outbound_message(message_id=message_id)
    state.reserve_outbound_conversation_message(message)
    state.close()
    return database, message


def _contract_store(store_kind: str, tmp_path):
    if store_kind == "memory":
        return InMemoryDurableStateStore()
    return SQLiteDurableStateStore(tmp_path / "contract.sqlite3")


def _close_contract_store(state: object) -> None:
    close = getattr(state, "close", None)
    if callable(close):
        close()
