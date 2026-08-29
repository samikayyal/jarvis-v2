from __future__ import annotations

import json

from jarvis_control_plane import (
    GmailApiWriteProvider,
    GoogleHttpResponse,
)

from .helpers import (
    _dispatcher,
    _proposal,
)


def test_live_provider_posts_only_raw_frozen_rfc822_message_and_thread_id() -> None:
    class Transport:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def request(self, **kwargs: object) -> GoogleHttpResponse:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return GoogleHttpResponse(
                    status_code=200,
                    headers={"Content-Type": "application/json"},
                    body=b'{"access_token":"live-token"}',
                )
            return GoogleHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"id": "sent-004", "threadId": "thread-001"}).encode(),
            )

    transport = Transport()
    provider = GmailApiWriteProvider(
        client_id="client-id", client_secret="client-secret", transport=transport
    )
    dispatcher = _dispatcher(provider)  # type: ignore[arg-type]

    dispatcher.dispatch(dispatcher.bind_proposal(_proposal(reply=True)))

    assert len(transport.calls) == 2
    send = transport.calls[1]
    assert send["method"] == "POST"
    assert str(send["url"]).endswith("/gmail/v1/users/me/messages/send")
    encoded = json.loads(bytes(send["body"] or b"").decode())["raw"]
    raw = __import__("base64").urlsafe_b64decode(encoded + "===").decode()
    assert "To: recipient@example.com" in raw
    assert "In-Reply-To: <source-001@example.com>" in raw
    assert "References: <root@example.com> <source-001@example.com>" in raw
    assert '"threadId":"thread-001"' in bytes(send["body"] or b"").decode()
