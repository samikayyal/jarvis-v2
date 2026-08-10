"""Ticket 13 pinned OpenWA/Baileys adapter contract."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from email.message import Message
from http.client import BadStatusLine, IncompleteRead
from io import BytesIO
from typing import Any, cast
from urllib.error import HTTPError

import pytest
from test_support import build_receiver_components

import jarvis_control_plane.openwa as openwa_module
from jarvis_control_plane import (
    ControlledOutboundConnector,
    InMemoryAuditBoundary,
    OpenWAConfig,
    OpenWAHttpError,
    OpenWAHttpResponse,
    OpenWAIngressWorker,
    OpenWAOutboundConnector,
    OpenWAReadinessProbe,
    OpenWAWebhookAdapter,
    OutboundReply,
    SQLiteAuditBoundary,
    SQLiteDurableStateStore,
    UrllibOpenWAHttpTransport,
    sign_body,
)
from jarvis_control_plane.openwa import ControlledOpenWAHttpTransport
from jarvis_control_plane.ports import AuditWriteError, OutboundConnectorError

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
SECRET = b"ticket13-webhook-secret"
SESSION_ID = "7316be1d-38d8-47c1-9d58-374f456b9629"
SESSION_NAME = "jarvis"
OPERATOR = "962790000000@c.us"


class _ControlledUrlOpener:
    def __init__(self, *, response: object | None = None, error: Exception | None = None):
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


def test_pinned_signed_webhook_drives_reply_correlation() -> None:
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=SESSION_ID,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket13",
    )
    raw_body = _raw_openwa_event()
    inbound = OpenWAWebhookAdapter(receiver=components.receiver)
    worker = OpenWAIngressWorker(
        receiver=components.receiver,
        state=components.state,
    )
    event_headers = {
        "Content-Type": "application/json",
        "X-OpenWA-Signature": f"sha256={sign_body(raw_body, SECRET)}",
        "X-OpenWA-Idempotency-Key": "message.received:session:wa-inbound-001",
        "X-OpenWA-Delivery-Id": "delivery-001",
    }

    acknowledgement = inbound.receive(raw_body=raw_body, headers=event_headers)
    unauthenticated = inbound.receive(
        raw_body=_raw_openwa_event(body="must not be admitted"),
        headers={"Content-Type": "application/json"},
    )

    try:
        assert unauthenticated.disposition == "unauthenticated"
        assert acknowledgement.disposition == "admitted"
        controlled_outbound = cast(ControlledOutboundConnector, components.outbound)
        assert controlled_outbound.sent == []
        result = worker.run_once()
    finally:
        assert components.trace_store is not None
        components.trace_store._close_writer_service()

    assert result is not None
    assert result.disposition == "completed"
    assert len(controlled_outbound.sent) == 1
    reply = controlled_outbound.sent[0]
    assert reply.session_id == SESSION_ID
    assert reply.recipient_id == OPERATOR
    assert reply.quoted_message_id == "wa-inbound-001"
    assert components.state.list_ingress_claims()[0].event_id == (
        "message.received:session:wa-inbound-001"
    )


def test_admitted_webhook_work_survives_state_store_reconstruction(tmp_path) -> None:
    database = tmp_path / "ticket13-ingress.sqlite3"
    first_connection = sqlite3.connect(database)
    first_state = SQLiteDurableStateStore(first_connection)
    first_audit = SQLiteAuditBoundary(first_connection)
    first = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=SESSION_ID,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket13-before-restart",
        state=first_state,
        audit=first_audit,
    )
    raw_body = _raw_openwa_event(body="finish after receiver reconstruction")
    acknowledgement = OpenWAWebhookAdapter(receiver=first.receiver).receive(
        raw_body=raw_body,
        headers={"X-OpenWA-Signature": f"sha256={sign_body(raw_body, SECRET)}"},
    )
    assert acknowledgement.disposition == "admitted"
    assert first.state.list_requests() == ()
    assert first.trace_store is not None
    first.trace_store._close_writer_service()
    first_state.close()
    first_audit.close()
    first_connection.close()

    second_connection = sqlite3.connect(database)
    second_state = SQLiteDurableStateStore(second_connection)
    second_audit = SQLiteAuditBoundary(second_connection)
    second = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=SESSION_ID,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket13-after-restart",
        state=second_state,
        audit=second_audit,
    )
    try:
        worker = OpenWAIngressWorker(
            receiver=second.receiver,
            state=second.state,
        )
        assert worker.startup_interrupted_count == 1
        assert worker.run_once() is None
        assert second.state.list_ingress_claims()[0].disposition == "interrupted"
        restart_evidence = tuple(
            record
            for record in second.audit.records
            if record.kind == "service_restart"
            and record.details.get("interrupted_ingress") == "nonterminal"
        )
        assert len(restart_evidence) == 1
    finally:
        assert second.trace_store is not None
        second.trace_store._close_writer_service()
        second_state.close()
        second_audit.close()
        second_connection.close()


def test_restart_interrupts_claimed_dispatch_without_replaying_it() -> None:
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=SESSION_ID,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket13-interrupted",
    )
    raw_body = _raw_openwa_event(body="do not replay an uncertain dispatch")
    acknowledgement = OpenWAWebhookAdapter(receiver=components.receiver).receive(
        raw_body=raw_body,
        headers={"X-OpenWA-Signature": f"sha256={sign_body(raw_body, SECRET)}"},
    )
    claimed = components.state.begin_next_ingress_dispatch()
    worker = OpenWAIngressWorker(
        receiver=components.receiver,
        state=components.state,
    )

    try:
        replay = worker.run_once()
    finally:
        assert components.trace_store is not None
        components.trace_store._close_writer_service()

    assert acknowledgement.disposition == "admitted"
    assert claimed is not None
    assert worker.startup_interrupted_count == 1
    assert replay is None
    assert components.state.list_ingress_claims()[0].disposition == "interrupted"
    controlled_outbound = cast(ControlledOutboundConnector, components.outbound)
    assert controlled_outbound.sent == []


def test_restart_reconciliation_fails_closed_when_audit_is_unavailable() -> None:
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=SESSION_ID,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket13-restart-audit-failure",
    )
    raw_body = _raw_openwa_event(body="retain until restart audit is durable")
    acknowledgement = OpenWAWebhookAdapter(receiver=components.receiver).receive(
        raw_body=raw_body,
        headers={"X-OpenWA-Signature": f"sha256={sign_body(raw_body, SECRET)}"},
    )
    audit = cast(InMemoryAuditBoundary, components.audit)
    audit.fail = True

    try:
        with pytest.raises(AuditWriteError, match="controlled audit append failure"):
            OpenWAIngressWorker(
                receiver=components.receiver,
                state=components.state,
            )
    finally:
        assert components.trace_store is not None
        components.trace_store._close_writer_service()

    assert acknowledgement.disposition == "admitted"
    assert components.state.list_ingress_claims()[0].disposition == "admitted"


def test_outbound_adapter_uses_pinned_reply_route_and_returned_message_id() -> None:
    transport = ControlledOpenWAHttpTransport(
        responses=(
            OpenWAHttpResponse(status_code=200, body=b'{"status":"ok"}'),
            OpenWAHttpResponse(
                status_code=200,
                body=(
                    b'{"id":"7316be1d-38d8-47c1-9d58-374f456b9629",'
                    b'"name":"jarvis","status":"ready"}'
                ),
            ),
            OpenWAHttpResponse(
                status_code=201,
                body=b'{"messageId":"wa-outbound-001","timestamp":1786363200}',
            ),
        )
    )
    config = _config()
    readiness = OpenWAReadinessProbe(config=config, transport=transport)
    connector = OpenWAOutboundConnector(
        config=config,
        transport=transport,
        readiness=readiness,
    )
    reply = OutboundReply(
        reply_id="reply-001",
        request_id="request-001",
        session_id=SESSION_ID,
        recipient_id=OPERATOR,
        quoted_message_id="wa-inbound-001",
        body="request_id=request-001 completed",
    )

    connector.preflight(reply)
    delivery = connector.send(reply)

    assert delivery.outbound_id == "wa-outbound-001"
    assert delivery.accepted is True
    request = transport.requests[-1]
    assert request.method == "POST"
    assert request.url.endswith(f"/sessions/{SESSION_ID}/messages/reply")
    assert request.headers == {
        "Content-Type": "application/json",
        "X-API-Key": "owa_k1_contract-test",
    }
    assert request.body is not None
    assert json.loads(request.body) == {
        "chatId": OPERATOR,
        "quotedMessageId": "wa-inbound-001",
        "text": "request_id=request-001 completed",
    }


def test_readiness_keeps_container_health_and_named_session_state_distinct() -> None:
    transport = ControlledOpenWAHttpTransport(
        responses=(
            OpenWAHttpResponse(status_code=200, body=b'{"status":"ok"}'),
            OpenWAHttpResponse(
                status_code=200,
                body=(
                    b'{"id":"7316be1d-38d8-47c1-9d58-374f456b9629",'
                    b'"name":"jarvis","status":"disconnected"}'
                ),
            ),
        )
    )

    state = OpenWAReadinessProbe(config=_config(), transport=transport).current()

    assert state.container_healthy is True
    assert state.named_session_status == "disconnected"
    assert state.messaging_ready is False
    assert transport.requests[0].url.endswith("/health/ready")
    assert transport.requests[0].headers == {}
    assert transport.requests[1].url.endswith(f"/sessions/{SESSION_ID}")
    assert transport.requests[1].headers == {"X-API-Key": "owa_k1_contract-test"}


def test_openwa_readiness_is_reflected_in_status_after_async_admission() -> None:
    transport = ControlledOpenWAHttpTransport(
        responses=(
            OpenWAHttpResponse(status_code=200, body=b'{"status":"ok"}'),
            OpenWAHttpResponse(
                status_code=200,
                body=(
                    b'{"id":"7316be1d-38d8-47c1-9d58-374f456b9629",'
                    b'"name":"jarvis","status":"ready"}'
                ),
            ),
        )
    )
    probe = OpenWAReadinessProbe(config=_config(), transport=transport)
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=SESSION_ID,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket13-status",
        messaging_readiness_provider=probe,
    )
    worker = OpenWAIngressWorker(
        receiver=components.receiver,
        state=components.state,
    )
    raw_body = _raw_openwa_event(body="/status")
    acknowledgement = OpenWAWebhookAdapter(receiver=components.receiver).receive(
        raw_body=raw_body,
        headers={"X-OpenWA-Signature": f"sha256={sign_body(raw_body, SECRET)}"},
    )

    try:
        result = worker.run_once()
    finally:
        assert components.trace_store is not None
        components.trace_store._close_writer_service()

    assert acknowledgement.disposition == "admitted"
    assert result is not None
    assert result.disposition == "status"
    controlled_outbound = cast(ControlledOutboundConnector, components.outbound)
    assert "OpenWA=ready" in controlled_outbound.sent[0].body


def test_outbound_envelope_is_fixed_and_ambiguous_send_is_not_definite() -> None:
    config = _config()
    transport = ControlledOpenWAHttpTransport(
        responses=(
            OpenWAHttpResponse(status_code=200, body=b'{"status":"ok"}'),
            OpenWAHttpResponse(
                status_code=200,
                body=(
                    b'{"id":"7316be1d-38d8-47c1-9d58-374f456b9629",'
                    b'"name":"jarvis","status":"ready"}'
                ),
            ),
            OpenWAHttpResponse(status_code=200, body=b'{"status":"ok"}'),
            OpenWAHttpResponse(
                status_code=200,
                body=(
                    b'{"id":"7316be1d-38d8-47c1-9d58-374f456b9629",'
                    b'"name":"jarvis","status":"ready"}'
                ),
            ),
        ),
        failures=(OpenWAHttpError("timeout", may_have_sent=True),),
    )
    connector = OpenWAOutboundConnector(
        config=config,
        transport=transport,
        readiness=OpenWAReadinessProbe(config=config, transport=transport),
    )
    bounded = OutboundReply(
        reply_id="reply-bounded",
        request_id="request-bounded",
        session_id=SESSION_ID,
        recipient_id=OPERATOR,
        quoted_message_id="wa-inbound-bounded",
        body="request_id=request-bounded "
        + "x" * (4096 - len("request_id=request-bounded ")),
    )
    connector.preflight(bounded)

    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=SESSION_ID,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket13-ambiguous",
        outbound=connector,
    )
    worker = OpenWAIngressWorker(
        receiver=components.receiver,
        state=components.state,
    )
    raw_body = _raw_openwa_event(body="summarize the ambiguous result")
    try:
        acknowledgement = OpenWAWebhookAdapter(receiver=components.receiver).receive(
            raw_body=raw_body,
            headers={
                "X-OpenWA-Signature": f"sha256={sign_body(raw_body, SECRET)}"
            },
        )
        result = worker.run_once()
    finally:
        assert components.trace_store is not None
        components.trace_store._close_writer_service()

    assert acknowledgement.disposition == "admitted"
    assert result is not None
    assert result.disposition == "unknown"
    assert components.state.list_outbound_conversation_attempts()[0].status.value == (
        "unknown"
    )
    assert len([request for request in transport.requests if request.method == "POST"]) == 1

    oversized = OutboundReply(
        reply_id="reply-oversized",
        request_id="request-oversized",
        session_id=SESSION_ID,
        recipient_id=OPERATOR,
        quoted_message_id="wa-inbound-oversized",
        body="x" * 4097,
    )
    with pytest.raises(OutboundConnectorError, match="4,096"):
        connector.preflight(oversized)


def test_urllib_transport_rejects_redirects_before_forwarding_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirect_headers = Message()
    redirect_headers["Location"] = "https://attacker.test/collect"
    redirect = HTTPError(
        "http://openwa.test:2785/api/messages",
        302,
        "Found",
        redirect_headers,
        BytesIO(b"redirect rejected"),
    )
    opener = _ControlledUrlOpener(error=redirect)
    installed_handlers: list[Any] = []

    def controlled_build_opener(*handlers: object) -> _ControlledUrlOpener:
        installed_handlers.extend(handlers)
        return opener

    monkeypatch.setattr(openwa_module, "build_opener", controlled_build_opener, raising=False)

    with pytest.raises(OpenWAHttpError) as raised:
        UrllibOpenWAHttpTransport().request(
            method="POST",
            url="http://openwa.test:2785/api/messages",
            headers={"X-API-Key": "owa_k1_must-not-redirect"},
            body=b"{}",
            timeout_seconds=5.0,
        )

    assert raised.value.code == "redirect_rejected"
    assert raised.value.may_have_sent is True
    assert len(opener.requests) == 1
    assert len(installed_handlers) == 1
    assert installed_handlers[0].redirect_request(None, None, 302, "", {}, "") is None


@pytest.mark.parametrize(
    ("opener_error", "response_error"),
    (
        (BadStatusLine("broken status"), None),
        (None, IncompleteRead(b"partial", 10)),
    ),
)
def test_urllib_transport_maps_protocol_failures_to_ambiguous_post_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    opener_error: Exception | None,
    response_error: Exception | None,
) -> None:
    response = None if response_error is None else _BrokenHttpResponse(response_error)
    opener = _ControlledUrlOpener(response=response, error=opener_error)
    monkeypatch.setattr(
        openwa_module,
        "build_opener",
        lambda *_handlers: opener,
        raising=False,
    )

    with pytest.raises(OpenWAHttpError) as raised:
        UrllibOpenWAHttpTransport().request(
            method="POST",
            url="http://openwa.test:2785/api/messages",
            headers={"X-API-Key": "owa_k1_protocol-failure"},
            body=b"{}",
            timeout_seconds=5.0,
        )

    assert raised.value.code == "invalid_response"
    assert raised.value.may_have_sent is True
    if response is not None:
        assert response.closed is True
