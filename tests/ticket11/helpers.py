from __future__ import annotations

from datetime import UTC, datetime

from jarvis_control_plane import InboundMessage, SignedInboundEvent, WorkerIdentity

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"
SECRET = b"ticket11-test-secret"


def _worker_identity(
    *,
    host: str = "ubuntu",
    worker_id: str = "ubuntu-01",
    connection_id: str = "boot-01",
) -> WorkerIdentity:
    return WorkerIdentity(host=host, worker_id=worker_id, connection_id=connection_id)


def _event(text: str, suffix: str) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id=f"event-worker-{suffix}",
            message_id=f"message-worker-{suffix}",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=text,
        ),
        SECRET,
    )
