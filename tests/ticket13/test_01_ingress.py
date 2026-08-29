from __future__ import annotations

import sqlite3
from typing import cast

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ControlledOutboundConnector,
    InMemoryAuditBoundary,
    OpenWAIngressWorker,
    OpenWAWebhookAdapter,
    SQLiteAuditBoundary,
    SQLiteDurableStateStore,
    sign_body,
)
from jarvis_control_plane.ports import AuditWriteError

from .helpers import NOW, OPERATOR, SECRET, SESSION_ID, _raw_openwa_event


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
