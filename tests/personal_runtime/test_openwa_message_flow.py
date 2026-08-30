from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Self
from urllib.request import Request

from jarvis_personal_runtime.openwa import (
    DeliveryDisposition,
    OpenWAHttpSender,
    OpenWAMessageFlow,
    OpenWASendError,
    OpenWASettings,
    WebhookDisposition,
)
from jarvis_personal_runtime.runtime import Completed, PersonalRuntime

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
SESSION_ID = "7316be1d-38d8-47c1-9d58-374f456b9629"
OPERATOR = "962790000000@c.us"
SECRET = b"replacement-openwa-secret"


class _Clock:
    def now(self) -> datetime:
        return NOW


class _BlockingRunner:
    def __init__(self, reply: str = "done") -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.reply = reply

    async def run(self, *_args: object, **_kwargs: object) -> Completed:
        self.entered.set()
        await self.release.wait()
        return Completed(self.reply)

    async def resume(self, *_args: object, **_kwargs: object) -> Completed:
        raise AssertionError("approval continuation was not expected")


class _RecordingSender:
    def __init__(self, fail_at: int | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_at = fail_at

    def send_text(self, chat_id: str, text: str) -> str:
        self.sent.append((chat_id, text))
        if len(self.sent) == self.fail_at:
            raise OpenWASendError("timeout", may_have_sent=True)
        return f"outbound-{len(self.sent)}"


def _settings() -> OpenWASettings:
    return OpenWASettings(
        api_base_url="http://openwa.test:2785/api",
        api_key="owa_k1_replacement",
        internal_session_id=SESSION_ID,
        authorized_sender=OPERATOR,
        operator_chat_id=OPERATOR,
    )


def _event(
    *,
    message_id: str = "wa-inbound-001",
    body: object = "hello",
    **data_overrides: object,
) -> bytes:
    data = {
        "id": message_id,
        "from": OPERATOR,
        "chatId": OPERATOR,
        "body": body,
        "type": "text",
        "fromMe": False,
        "isGroup": False,
    }
    data.update(data_overrides)
    return json.dumps(
        {
            "event": "message.received",
            "sessionId": SESSION_ID,
            "idempotencyKey": f"message.received:session:{message_id}",
            "deliveryId": "delivery-001",
            "data": data,
        },
        separators=(",", ":"),
    ).encode()


def _headers(raw_body: bytes) -> dict[str, str]:
    signature = hmac.new(SECRET, raw_body, hashlib.sha256).hexdigest()
    return {"X-OpenWA-Signature": f"sha256={signature}"}


def test_signed_direct_text_is_acknowledged_before_runtime_work_finishes() -> None:
    async def scenario() -> None:
        runner = _BlockingRunner()
        sender = _RecordingSender()
        runtime = PersonalRuntime(request_runner=runner, clock=_Clock())
        flow = OpenWAMessageFlow(
            settings=_settings(),
            signing_secret=SECRET,
            runtime=runtime,
            sender=sender,
            clock=_Clock(),
        )
        raw_body = _event()

        acknowledgement = flow.receive_webhook(raw_body, _headers(raw_body))

        assert acknowledgement.status_code == 202
        assert acknowledgement.disposition is WebhookDisposition.ADMITTED
        assert runner.entered.is_set() is False

        await asyncio.wait_for(runner.entered.wait(), timeout=1)
        assert sender.sent == []
        runner.release.set()
        outcomes = await flow.drain()

        assert outcomes[0].message_id == "wa-inbound-001"
        assert sender.sent == [(OPERATOR, "done")]

    asyncio.run(scenario())


def test_reply_chunks_are_sent_once_each_in_deterministic_order() -> None:
    async def scenario() -> None:
        runner = _BlockingRunner("alpha beta gamma delta")
        runner.release.set()
        sender = _RecordingSender()
        settings = OpenWASettings(
            api_base_url="http://openwa.test:2785/api",
            api_key="owa_k1_replacement",
            internal_session_id=SESSION_ID,
            authorized_sender=OPERATOR,
            operator_chat_id=OPERATOR,
            max_text_characters=12,
        )
        flow = OpenWAMessageFlow(
            settings=settings,
            signing_secret=SECRET,
            runtime=PersonalRuntime(request_runner=runner, clock=_Clock()),
            sender=sender,
            clock=_Clock(),
        )
        raw_body = _event()

        flow.receive_webhook(raw_body, _headers(raw_body))
        outcomes = await flow.drain()

        assert sender.sent == [
            (OPERATOR, "alpha beta"),
            (OPERATOR, "gamma delta"),
        ]
        assert outcomes[0].outbound_ids == ("outbound-1", "outbound-2")
        assert outcomes[0].delivery is DeliveryDisposition.SENT

    asyncio.run(scenario())


def test_uncertain_chunk_is_not_retried_and_later_chunks_are_not_attempted() -> None:
    async def scenario() -> None:
        runner = _BlockingRunner("alpha beta gamma delta epsilon")
        runner.release.set()
        sender = _RecordingSender(fail_at=2)
        settings = OpenWASettings(
            api_base_url="http://openwa.test:2785/api",
            api_key="owa_k1_replacement",
            internal_session_id=SESSION_ID,
            authorized_sender=OPERATOR,
            operator_chat_id=OPERATOR,
            max_text_characters=12,
        )
        flow = OpenWAMessageFlow(
            settings=settings,
            signing_secret=SECRET,
            runtime=PersonalRuntime(request_runner=runner, clock=_Clock()),
            sender=sender,
            clock=_Clock(),
        )
        raw_body = _event()

        flow.receive_webhook(raw_body, _headers(raw_body))
        outcomes = await flow.drain()

        assert sender.sent == [
            (OPERATOR, "alpha beta"),
            (OPERATOR, "gamma delta"),
        ]
        assert outcomes[0].outbound_ids == ("outbound-1",)
        assert outcomes[0].delivery is DeliveryDisposition.UNKNOWN

    asyncio.run(scenario())


class _Response:
    def __init__(self, body: bytes, status: int = 201) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, _limit: int = -1) -> bytes:
        return self.body


def test_http_sender_preserves_the_verified_openwa_send_text_contract() -> None:
    requests: list[Request] = []

    def opener(request: Request, *, timeout: float) -> _Response:
        assert timeout == 5.0
        requests.append(request)
        return _Response(b'{"messageId":"wa-outbound-001"}')

    sender = OpenWAHttpSender(_settings(), opener=opener)

    outbound_id = sender.send_text(OPERATOR, "hello from replacement")

    assert outbound_id == "wa-outbound-001"
    assert len(requests) == 1
    request = requests[0]
    assert request.get_method() == "POST"
    assert request.full_url.endswith(f"/sessions/{SESSION_ID}/messages/send-text")
    assert dict(request.header_items()) == {
        "X-api-key": "owa_k1_replacement",
        "Content-type": "application/json",
    }
    assert json.loads(request.data) == {
        "chatId": OPERATOR,
        "text": "hello from replacement",
    }


def test_duplicate_admitted_message_is_suppressed_without_a_second_send() -> None:
    async def scenario() -> None:
        runner = _BlockingRunner()
        runner.release.set()
        sender = _RecordingSender()
        flow = OpenWAMessageFlow(
            settings=_settings(),
            signing_secret=SECRET,
            runtime=PersonalRuntime(request_runner=runner, clock=_Clock()),
            sender=sender,
            clock=_Clock(),
        )
        raw_body = _event()

        first = flow.receive_webhook(raw_body, _headers(raw_body))
        await flow.drain()
        second = flow.receive_webhook(raw_body, _headers(raw_body))
        outcomes = await flow.drain()

        assert first.disposition is WebhookDisposition.ADMITTED
        assert second.disposition is WebhookDisposition.ADMITTED
        assert outcomes[0].runtime_result.disposition.value == "duplicate"
        assert sender.sent == [(OPERATOR, "done")]

    asyncio.run(scenario())


def test_unauthenticated_webhook_is_rejected_without_runtime_work() -> None:
    async def scenario() -> None:
        runner = _BlockingRunner()
        sender = _RecordingSender()
        flow = OpenWAMessageFlow(
            settings=_settings(),
            signing_secret=SECRET,
            runtime=PersonalRuntime(request_runner=runner, clock=_Clock()),
            sender=sender,
            clock=_Clock(),
        )

        acknowledgement = flow.receive_webhook(
            _event(body="do not admit"),
            {"X-OpenWA-Signature": "sha256=wrong"},
        )

        assert acknowledgement.status_code == 401
        assert acknowledgement.disposition is WebhookDisposition.UNAUTHENTICATED
        assert await flow.drain() == ()
        assert runner.entered.is_set() is False
        assert sender.sent == []

    asyncio.run(scenario())


def test_authenticated_payload_missing_basic_text_shape_is_rejected() -> None:
    async def scenario() -> None:
        runner = _BlockingRunner()
        flow = OpenWAMessageFlow(
            settings=_settings(),
            signing_secret=SECRET,
            runtime=PersonalRuntime(request_runner=runner, clock=_Clock()),
            sender=_RecordingSender(),
            clock=_Clock(),
        )
        raw_body = _event(body=None)

        acknowledgement = flow.receive_webhook(raw_body, _headers(raw_body))

        assert acknowledgement.status_code == 400
        assert acknowledgement.disposition is WebhookDisposition.INVALID
        assert await flow.drain() == ()
        assert runner.entered.is_set() is False

    asyncio.run(scenario())


def test_excluded_openwa_traffic_is_acknowledged_without_runtime_work() -> None:
    async def scenario() -> None:
        runner = _BlockingRunner()
        sender = _RecordingSender()
        flow = OpenWAMessageFlow(
            settings=_settings(),
            signing_secret=SECRET,
            runtime=PersonalRuntime(request_runner=runner, clock=_Clock()),
            sender=sender,
            clock=_Clock(),
        )
        excluded = (
            {"from": "962799999999@c.us", "chatId": "962799999999@c.us"},
            {"isGroup": True, "chatId": "120363000000@g.us"},
            {"fromMe": True},
            {"type": "image", "body": "caption"},
        )

        acknowledgements = []
        for index, overrides in enumerate(excluded):
            raw_body = _event(message_id=f"ignored-{index}", **overrides)
            acknowledgements.append(flow.receive_webhook(raw_body, _headers(raw_body)))

        assert all(item.status_code == 202 for item in acknowledgements)
        assert all(
            item.disposition is WebhookDisposition.IGNORED for item in acknowledgements
        )
        assert await flow.drain() == ()
        assert runner.entered.is_set() is False
        assert sender.sent == []

    asyncio.run(scenario())
