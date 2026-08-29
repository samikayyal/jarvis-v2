from __future__ import annotations

import json
from datetime import UTC, datetime

from jarvis_control_plane import OpenWAConfig

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
SECRET = b"ticket13-webhook-secret"
SESSION_ID = "7316be1d-38d8-47c1-9d58-374f456b9629"
SESSION_NAME = "jarvis"
OPERATOR = "962790000000@c.us"


class _ControlledUrlOpener:
    def __init__(
        self, *, response: object | None = None, error: Exception | None = None
    ):
        self.response = response
        self.error = error
        self.requests: list[object] = []

    def open(self, request: object, *, timeout: float) -> object:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class _BrokenHttpResponse:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.closed = False

    def getcode(self) -> int:
        return 201

    def read(self, _limit: int = -1) -> bytes:
        raise self.error

    def close(self) -> None:
        self.closed = True


def _raw_openwa_event(*, body: str = "summarize the controlled result") -> bytes:
    return json.dumps(
        {
            "event": "message.received",
            "timestamp": "2026-08-10T12:00:00.000Z",
            "sessionId": SESSION_ID,
            "idempotencyKey": "message.received:session:wa-inbound-001",
            "deliveryId": "delivery-001",
            "data": {
                "id": "wa-inbound-001",
                "from": OPERATOR,
                "to": "962790000001@c.us",
                "chatId": OPERATOR,
                "body": body,
                "type": "text",
                "fromMe": False,
                "isGroup": False,
                "isLidSender": False,
            },
        },
        separators=(",", ":"),
    ).encode()


def _config() -> OpenWAConfig:
    return OpenWAConfig(
        api_base_url="http://openwa.test:2785/api",
        api_key="owa_k1_contract-test",
        internal_session_id=SESSION_ID,
        named_session=SESSION_NAME,
        operator_conversation_id=OPERATOR,
    )
