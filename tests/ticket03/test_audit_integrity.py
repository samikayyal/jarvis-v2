# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from jarvis_control_plane import (
    AuditEvidence,
    AuditFilter,
    AuditWriteError,
    ControlledOrchestrationAdapter,
    ControlPlaneConfig,
    DeterministicCapabilityBroker,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    FixedModelAvailabilityProvider,
    InboundMessage,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryDurableStateStore,
    ModelAvailability,
    OutboundConnectorError,
    SignedInboundEvent,
    SignedMessageReceiver,
    SQLiteAuditBoundary,
)
from jarvis_control_plane.models import OutboundDelivery

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
SECRET = b"ticket03-test-secret"
OPERATOR = "operator.test"
SESSION = "session.test"
_TRACE_STORE: InMemoryDiagnosticTraceStore | None = None
_TRACE_WRITER: object | None = None


def make_trace(
    clock: FixedClock, ids: DeterministicIdGenerator
) -> DiagnosticTraceRecorder:
    global _TRACE_STORE, _TRACE_WRITER
    if _TRACE_WRITER is None:
        _TRACE_STORE = InMemoryDiagnosticTraceStore()
        _TRACE_WRITER = _TRACE_STORE.writer()
    return DiagnosticTraceRecorder(writer=_TRACE_WRITER, clock=clock, ids=ids)


@pytest.fixture(scope="module", autouse=True)
def close_module_trace_store():
    global _TRACE_STORE, _TRACE_WRITER
    yield
    if _TRACE_STORE is not None:
        _TRACE_STORE._close_writer_service()
    _TRACE_WRITER = None
    _TRACE_STORE = None


def make_evidence(
    evidence_id: str,
    occurred_at: datetime,
    **changes: object,
) -> AuditEvidence:
    values: dict[str, object] = {
        "evidence_id": evidence_id,
        "kind": "action_outcome",
        "occurred_at": occurred_at,
        "event_id": "event-001",
        "request_id": "request-001",
        "message_id": "message-001",
        "operation_type": "gmail_send",
        "target_category": "google_gmail",
        "approval_decision": "approved",
        "policy_decision": "allowed",
        "execution_status": "accepted",
        "outcome": "accepted",
        "actor": "capability_broker",
        "details": {"channel": "controlled", "command": "--token=not-retained"},
    }
    values.update(changes)
    return AuditEvidence(**values)  # type: ignore[arg-type]


def make_event(
    config: ControlPlaneConfig,
    *,
    event_id: str = "event-001",
    message_id: str = "message-001",
) -> SignedInboundEvent:
    message = InboundMessage(
        event_type="message.received",
        session_id=SESSION,
        event_id=event_id,
        message_id=message_id,
        sender_id=OPERATOR,
        chat_id=OPERATOR,
        chat_type="direct",
        message_type="text",
        from_me=False,
        text="run the controlled request",
    )
    return SignedInboundEvent.from_message(message, config.signing_secret)


class PlainOutbound:
    def __init__(self) -> None:
        self.sent = []

    def preflight(self, reply: object) -> None:
        del reply

    def send(self, reply: object) -> OutboundDelivery:
        self.sent.append(reply)
        return OutboundDelivery(
            outbound_id=f"plain-outbound-{len(self.sent)}", accepted=True
        )


def test_audit_failure_at_ingress_preserves_the_replay_key() -> None:
    config = ControlPlaneConfig(
        operator_id=OPERATOR,
        session_id=SESSION,
        signing_secret=SECRET,
    )
    state = InMemoryDurableStateStore()
    audit = InMemoryAuditBoundary(fail=True)
    clock = FixedClock(NOW)
    ids = DeterministicIdGenerator("ticket03")
    outbound = PlainOutbound()
    trace = make_trace(clock, ids)
    broker = DeterministicCapabilityBroker(
        config=config,
        state=state,
        audit=audit,
        orchestration=ControlledOrchestrationAdapter(),
        outbound=outbound,  # type: ignore[arg-type]
        clock=clock,
        ids=ids,
        trace=trace,
        model_availability_provider=FixedModelAvailabilityProvider(ModelAvailability()),
    )
    receiver = SignedMessageReceiver(
        config=config,
        state=state,
        audit=audit,
        broker=broker,
        clock=clock,
        ids=ids,
    )

    result = receiver.receive(make_event(config))

    assert result.disposition == "audit_blocked"
    assert len(state.list_ingress_claims()) == 1
    assert outbound.sent == []
    audit.fail = False
    retry = receiver.receive(make_event(config))
    assert retry.disposition == "duplicate"
    assert outbound.sent == []


def test_request_audit_failure_rolls_back_request_and_preserves_replay_claim() -> None:
    config = ControlPlaneConfig(
        operator_id=OPERATOR,
        session_id=SESSION,
        signing_secret=SECRET,
    )
    state = InMemoryDurableStateStore()
    audit = InMemoryAuditBoundary(fail_on_append=2)
    clock = FixedClock(NOW)
    ids = DeterministicIdGenerator("ticket03")
    outbound = PlainOutbound()
    trace = make_trace(clock, ids)
    broker = DeterministicCapabilityBroker(
        config=config,
        state=state,
        audit=audit,
        orchestration=ControlledOrchestrationAdapter(),
        outbound=outbound,  # type: ignore[arg-type]
        clock=clock,
        ids=ids,
        trace=trace,
        model_availability_provider=FixedModelAvailabilityProvider(ModelAvailability()),
    )
    receiver = SignedMessageReceiver(
        config=config,
        state=state,
        audit=audit,
        broker=broker,
        clock=clock,
        ids=ids,
    )

    blocked = receiver.receive(make_event(config))

    assert blocked.disposition == "audit_blocked"
    assert state.list_requests() == ()
    assert len(state.list_ingress_claims()) == 1
    assert outbound.sent == []

    audit.fail_on_append = None
    recovered = receiver.receive(
        make_event(config, event_id="event-002", message_id="message-002")
    )

    assert recovered.disposition == "completed"
    assert len(state.list_requests()) == 1
    assert len(state.list_ingress_claims()) == 2
    assert outbound.sent == [recovered.reply]


def test_safe_local_reads_remain_available_when_append_is_down() -> None:
    audit = InMemoryAuditBoundary()
    evidence = make_evidence("audit-001", NOW)
    audit.append(evidence)
    audit.fail = True

    assert audit.safe_view(AuditFilter(request_id="request-001")) == (evidence,)
    assert json.loads(audit.export_json(request_id="request-001")) == [
        evidence.as_safe_mapping()
    ]


def test_ticket01_sqlite_audit_schema_is_reconstructed_before_ticket03_use(
    tmp_path,
) -> None:
    database = tmp_path / "legacy-ticket01-audit.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE audit_evidence (
            evidence_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            event_id TEXT,
            request_id TEXT,
            outcome TEXT NOT NULL,
            actor TEXT NOT NULL,
            details_json TEXT NOT NULL,
            redacted INTEGER NOT NULL CHECK (redacted = 1)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO audit_evidence(
            evidence_id, kind, occurred_at, event_id, request_id,
            outcome, actor, details_json, redacted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            "legacy-audit-001",
            "action_outcome",
            NOW.isoformat(),
            "legacy-event-001",
            "legacy-request-001",
            "accepted",
            "capability_broker",
            json.dumps({"channel": "controlled"}),
        ),
    )
    connection.commit()
    connection.close()

    audit = SQLiteAuditBoundary(database)
    try:
        evidence = make_evidence(
            "ticket03-after-legacy-migration",
            NOW + timedelta(minutes=1),
            request_id="request-after-migration",
        )
        audit.append(evidence)
        assert audit.safe_view(request_id="request-after-migration") == (evidence,)
        exported = json.loads(audit.export_json(request_id="request-after-migration"))
        assert exported == [evidence.as_safe_mapping()]
        assert audit.safe_view(request_id="legacy-request-001")[0].evidence_id == (
            "legacy-audit-001"
        )
    finally:
        audit.close()


def test_safe_views_are_bounded_by_default_for_both_adapters() -> None:
    records = tuple(make_evidence(f"audit-{index:04d}", NOW) for index in range(1_001))
    memory = InMemoryAuditBoundary()
    memory.append_batch(records)
    assert AuditFilter().limit == 1_000
    assert len(memory.safe_view()) == 1_000
    assert len(json.loads(memory.export_json())) == 1_000

    sqlite_audit = SQLiteAuditBoundary(":memory:")
    sqlite_audit.append_batch(records)
    assert len(sqlite_audit.safe_view()) == 1_000
    assert len(sqlite_audit.safe_view(operation_type="gmail_send")) == 1_000
    assert len(json.loads(sqlite_audit.export_json())) == 1_000
    sqlite_audit.close()


def test_sqlite_safe_export_matches_in_memory_and_rejects_uncommitted_shared_work(
    tmp_path,
) -> None:
    evidence = make_evidence(
        "audit-001",
        NOW,
        details={"result": "accepted", "channel": "controlled"},
    )
    memory = InMemoryAuditBoundary()
    memory.append(evidence)

    database = tmp_path / "audit.sqlite3"
    sqlite_audit = SQLiteAuditBoundary(database)
    sqlite_audit.append(evidence)
    assert sqlite_audit.export_json() == memory.export_json()
    sqlite_audit.close()

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE pending_work (value TEXT)")
    connection.execute("INSERT INTO pending_work VALUES ('pending')")
    with pytest.raises(AuditWriteError, match="uncommitted transaction"):
        SQLiteAuditBoundary(connection)
    connection.rollback()

    shared = sqlite3.connect(":memory:")
    shared_audit = SQLiteAuditBoundary(shared)
    with pytest.raises(sqlite3.DatabaseError):
        shared.execute(
            """
            INSERT INTO audit_evidence(
                evidence_id, kind, occurred_at, outcome, actor, details_json, redacted
            ) VALUES ('forged', 'invalid', '2026-08-04T12:00:00+00:00',
                      'recorded', 'forged', '{}', 1)
            """
        )
    shared.execute("CREATE TABLE pending_after_init (value TEXT)")
    shared.execute("INSERT INTO pending_after_init VALUES ('pending')")
    with pytest.raises(AuditWriteError, match="uncommitted transaction"):
        shared_audit.append(evidence)
    shared.rollback()
    assert shared.execute("SELECT COUNT(*) FROM pending_after_init").fetchone()[0] == 0
    shared_audit.close()

    race = sqlite3.connect(":memory:")
    race_audit = SQLiteAuditBoundary(race)
    original_integrity_check = race_audit._assert_schema_integrity
    transaction_seen_before_integrity = False

    def begin_unrelated_work_after_check() -> None:
        nonlocal transaction_seen_before_integrity
        original_integrity_check()
        transaction_seen_before_integrity = race.in_transaction

    race_audit._assert_schema_integrity = begin_unrelated_work_after_check  # type: ignore[method-assign]
    race_audit.append(evidence)
    assert transaction_seen_before_integrity is True
    race_audit.close()


def test_sqlite_schema_tamper_fails_closed_for_safe_reads(tmp_path) -> None:
    database = tmp_path / "audit.sqlite3"
    audit = SQLiteAuditBoundary(database)
    audit.append(make_evidence("audit-001", NOW))

    external = sqlite3.connect(database)
    external.execute("DROP TRIGGER audit_evidence_no_update")
    external.commit()
    external.close()

    with pytest.raises(AuditWriteError, match="schema integrity"):
        audit.safe_view()
    audit.close()


def test_sqlite_inert_append_only_trigger_fails_closed(tmp_path) -> None:
    database = tmp_path / "audit.sqlite3"
    audit = SQLiteAuditBoundary(database)
    audit.append(make_evidence("audit-001", NOW))

    external = sqlite3.connect(database)
    external.execute("DROP TRIGGER audit_evidence_no_update")
    external.execute(
        """
        CREATE TRIGGER audit_evidence_no_update
        BEFORE UPDATE ON audit_evidence
        BEGIN
            SELECT 'audit evidence is append-only';
        END
        """
    )
    external.commit()
    external.close()

    with pytest.raises(AuditWriteError, match="schema integrity"):
        audit.safe_view()
    audit.close()


def test_sqlite_behavior_changing_table_ddl_fails_closed(tmp_path) -> None:
    database = tmp_path / "audit.sqlite3"
    audit = SQLiteAuditBoundary(database)
    audit.append(make_evidence("audit-001", NOW))

    external = sqlite3.connect(database)
    external.executescript(
        """
        DROP TRIGGER audit_evidence_no_update;
        DROP TRIGGER audit_evidence_no_delete;
        DROP TABLE audit_evidence;
        CREATE TABLE audit_evidence (
            evidence_id TEXT PRIMARY KEY ON CONFLICT IGNORE,
            kind TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            event_id TEXT,
            request_id TEXT,
            message_id TEXT,
            operation_type TEXT,
            target_category TEXT,
            approval_decision TEXT,
            policy_decision TEXT,
            execution_status TEXT,
            outcome TEXT NOT NULL,
            actor TEXT NOT NULL,
            details_json TEXT NOT NULL,
            redacted INTEGER NOT NULL CHECK (redacted = 1)
        );
        CREATE TRIGGER audit_evidence_no_update
            BEFORE UPDATE ON audit_evidence
            BEGIN
                SELECT RAISE(ABORT, 'audit evidence is append-only');
            END;
        CREATE TRIGGER audit_evidence_no_delete
            BEFORE DELETE ON audit_evidence
            BEGIN
                SELECT RAISE(ABORT, 'audit evidence is append-only');
            END;
        """
    )
    external.commit()
    external.close()

    with pytest.raises(AuditWriteError, match="schema integrity"):
        audit.safe_view()
    audit.close()


def test_sqlite_extra_trigger_fails_closed(tmp_path) -> None:
    database = tmp_path / "audit.sqlite3"
    audit = SQLiteAuditBoundary(database)
    audit.append(make_evidence("audit-001", NOW))

    external = sqlite3.connect(database)
    external.execute(
        """
        CREATE TRIGGER forged_audit_insert
        AFTER INSERT ON audit_evidence
        BEGIN
            INSERT INTO audit_evidence(
                evidence_id, kind, occurred_at, outcome, actor, details_json, redacted
            ) VALUES ('forged', 'invalid', '2026-08-04T12:00:00+00:00',
                      'recorded', 'forged', '{}', 1);
        END
        """
    )
    external.commit()
    external.close()

    with pytest.raises(AuditWriteError, match="schema integrity"):
        audit.safe_view()
    audit.close()
