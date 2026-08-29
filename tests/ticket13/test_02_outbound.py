from __future__ import annotations

import json
from typing import cast

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ControlledOpenWAHttpTransport,
    ControlledOutboundConnector,
    OpenWAHttpError,
    OpenWAHttpResponse,
    OpenWAIngressWorker,
    OpenWAOutboundConnector,
    OpenWAReadinessProbe,
    OpenWAWebhookAdapter,
    OutboundReply,
    sign_body,
)
from jarvis_control_plane.ports import OutboundConnectorError

from .helpers import NOW, OPERATOR, SECRET, SESSION_ID, _config, _raw_openwa_event


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
            headers={"X-OpenWA-Signature": f"sha256={sign_body(raw_body, SECRET)}"},
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
    assert (
        len([request for request in transport.requests if request.method == "POST"])
        == 1
    )

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
