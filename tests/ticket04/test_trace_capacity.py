# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

import gc
import inspect
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    MAX_TRACE_RESERVATION_BYTES,
    ControlledOrchestrationAdapter,
    ControlledOutboundConnector,
    ControlPlaneConfig,
    DeterministicCapabilityBroker,
    DeterministicIdGenerator,
    DiagnosticTraceLimits,
    DiagnosticTraceRecorder,
    FixedClock,
    FixedModelAvailabilityProvider,
    InboundMessage,
    InMemoryDiagnosticTraceStore,
    ModelAvailability,
    SignedInboundEvent,
    SignedMessageReceiver,
    SQLiteDiagnosticTraceStore,
    TraceCapacityError,
)
from jarvis_control_plane.manual_admin import (
    ManualDiagnosticTraceBoundary,
    _open_manual_trace_boundary,
    open_sqlite_manual_trace_boundary,
)
from jarvis_control_plane.traces import _DiagnosticTraceStoreBase
from jarvis_control_plane.writer_capability import _read_response_until_ready

SECRET = "credential-like-value-that-must-remain-verbatim"
SIGNING_SECRET = b"ticket04-signing-secret"
OPERATOR = "operator.test"
SESSION = "session.test"
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
_DEFAULT_TRACE_STORE: InMemoryDiagnosticTraceStore | None = None
_DEFAULT_TRACE_WRITER: object | None = None
_TEST_TRACE_STORES: list[InMemoryDiagnosticTraceStore] = []


def make_default_trace(
    clock: FixedClock, ids: DeterministicIdGenerator
) -> DiagnosticTraceRecorder:
    global _DEFAULT_TRACE_STORE, _DEFAULT_TRACE_WRITER
    if _DEFAULT_TRACE_WRITER is None:
        _DEFAULT_TRACE_STORE = InMemoryDiagnosticTraceStore()
        _DEFAULT_TRACE_WRITER = _DEFAULT_TRACE_STORE.writer()
    return DiagnosticTraceRecorder(
        writer=_DEFAULT_TRACE_WRITER,
        clock=clock,
        ids=ids,
    )


@pytest.fixture(scope="module", autouse=True)
def close_module_default_trace_store():
    global _DEFAULT_TRACE_STORE, _DEFAULT_TRACE_WRITER
    yield
    if _DEFAULT_TRACE_STORE is not None:
        _DEFAULT_TRACE_STORE._close_writer_service()
    _DEFAULT_TRACE_WRITER = None
    _DEFAULT_TRACE_STORE = None


def new_trace_store(**kwargs: int) -> InMemoryDiagnosticTraceStore:
    store = InMemoryDiagnosticTraceStore(**kwargs)
    _TEST_TRACE_STORES.append(store)
    return store


@pytest.fixture(autouse=True)
def close_test_trace_stores():
    yield
    for store in _TEST_TRACE_STORES:
        store._close_writer_service()
    _TEST_TRACE_STORES.clear()


class TraceMode(Enum):
    LIVE = "live"


@dataclass(frozen=True)
class TypedTracePayload:
    when: datetime
    path: Path
    values: tuple[str, ...]


class FixedCapacityProvider:
    def __init__(self, available: int) -> None:
        self.available = available

    def available_bytes(self) -> int:
        return self.available

    def reserve(self, amount: int) -> None:
        if amount > self.available:
            raise TraceCapacityError(
                "test filesystem capacity is insufficient",
                requested_bytes=amount,
                available_bytes=self.available,
            )
        self.available -= amount

    def release(self, amount: int) -> None:
        self.available += amount


def mapping_value(value: object, key: str) -> object:
    """Read a value from the explicit, collision-safe mapping envelope."""

    assert hasattr(value, "__getitem__")
    assert value["__type__"] == "mapping"  # type: ignore[index]
    for item in value["items"]:  # type: ignore[index]
        if item["key"] == key:
            return item["value"]
    raise AssertionError(f"mapping key not found: {key}")


def bounded_reachable(root: object, *, max_nodes: int = 256) -> list[object]:
    """Walk the capability graph without descending into imported modules/types."""

    pending = [root]
    visited: set[int] = set()
    reachable: list[object] = []
    while pending and len(reachable) < max_nodes:
        value = pending.pop()
        if id(value) in visited:
            continue
        visited.add(id(value))
        reachable.append(value)
        if isinstance(value, (str, bytes, int, float, bool, type(None))):
            continue
        if inspect.ismodule(value) or inspect.isclass(value) or inspect.iscode(value):
            continue
        if inspect.ismethod(value):
            pending.extend((value.__self__, value.__func__))
            continue
        if inspect.isfunction(value):
            pending.append(value.__globals__)
            if value.__closure__:
                pending.extend(
                    cell.cell_contents
                    for cell in value.__closure__
                    if cell.cell_contents is not None
                )
            continue
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
            continue
        pending.extend(gc.get_referents(value))
    return reachable


def make_message(**changes: object) -> InboundMessage:
    values: dict[str, object] = {
        "event_type": "message.received",
        "session_id": SESSION,
        "event_id": "event-004",
        "message_id": "message-004",
        "sender_id": OPERATOR,
        "chat_id": OPERATOR,
        "chat_type": "direct",
        "message_type": "text",
        "from_me": False,
        "text": "Run the controlled operation",
    }
    values.update(changes)
    return InboundMessage(**values)  # type: ignore[arg-type]


def make_components(
    *,
    trace: DiagnosticTraceRecorder | None = None,
    state: object | None = None,
    audit: object | None = None,
    orchestration: ControlledOrchestrationAdapter | None = None,
) -> tuple[
    ControlPlaneConfig,
    object,
    object,
    ControlledOrchestrationAdapter,
    ControlledOutboundConnector,
    SignedMessageReceiver,
]:
    clock = FixedClock(NOW)
    ids = DeterministicIdGenerator("ticket04")
    if trace is None:
        trace = make_default_trace(clock, ids)
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=SESSION,
        signing_secret=SIGNING_SECRET,
        now=NOW,
        id_prefix="ticket04",
        state=state,
        audit=audit,
        orchestration=orchestration,
        clock=clock,
        ids=ids,
        trace=trace,
    )
    return (
        components.config,
        components.state,
        components.audit,
        components.orchestration,
        components.outbound,
        components.receiver,
    )


def make_event(config: ControlPlaneConfig, **changes: object) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        make_message(**changes), config.signing_secret
    )


def test_broker_traces_model_and_connector_payloads_without_leaking_into_audit() -> (
    None
):
    store = new_trace_store()
    clock = FixedClock(NOW)
    ids = DeterministicIdGenerator("broker-trace")
    recorder = DiagnosticTraceRecorder(writer=store.writer(), clock=clock, ids=ids)
    orchestration = ControlledOrchestrationAdapter(
        response_text=f"Completed with {SECRET}"
    )
    config, _, audit, orchestration, outbound, receiver = make_components(
        trace=recorder,
        orchestration=orchestration,
    )

    result = receiver.receive(make_event(config, text=f"Use {SECRET}"))

    assert result.disposition == "completed"
    assert len(orchestration.calls) == 1
    assert len(outbound.sent) == 1
    traces = _open_manual_trace_boundary(store).list_traces(
        request_id=result.request_id
    )
    assert [trace.operation_type for trace in traces] == ["model", "connector"]
    assert all(SECRET in str(trace.to_mapping()) for trace in traces)
    assert all(SECRET not in str(record.details) for record in audit.records)


def test_broker_never_retains_a_readable_trace_store() -> None:
    config, state, audit, orchestration, outbound, receiver = make_components()
    broker = receiver.broker
    reachable = bounded_reachable(broker, max_nodes=512)

    with pytest.raises(TypeError):
        DeterministicCapabilityBroker(
            config=config,
            state=state,  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            orchestration=orchestration,
            outbound=outbound,
            clock=FixedClock(NOW),
            ids=DeterministicIdGenerator("missing-trace"),
            model_availability_provider=FixedModelAvailabilityProvider(
                ModelAvailability()
            ),
        )

    assert not hasattr(broker, "_default_trace_store")
    assert not any(
        callable(getattr(value, method, None))
        for value in reachable
        for method in ("read_traces", "_read_persisted_traces")
    )
    assert not any(isinstance(value, _DiagnosticTraceStoreBase) for value in reachable)
    assert config.session_id == SESSION


def test_trace_capacity_rejects_before_connector_and_preserves_prior_trace() -> None:
    store = new_trace_store(
        capacity_bytes=102_000,
        reservation_bytes=200_000,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("capacity"),
        reservation_bytes=100_000,
    )
    config, _, _, orchestration, outbound, receiver = make_components(trace=recorder)

    result = receiver.receive(make_event(config))

    assert result.disposition == "failed"
    assert result.request is not None
    assert result.request.outcome == "trace_unavailable"
    assert len(orchestration.calls) == 1
    assert outbound.sent == []
    traces = _open_manual_trace_boundary(store).list_traces(
        request_id=result.request_id
    )
    assert [trace.operation_type for trace in traces] == ["model"]
    assert store.retained_bytes > 0
    assert store.available_bytes < store.limits.reservation_bytes


def test_capacity_failure_happens_before_operation_starts() -> None:
    store = new_trace_store(
        capacity_bytes=64,
        reservation_bytes=128,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("capacity-before"),
    )
    started: list[bool] = []

    with pytest.raises(TraceCapacityError):
        recorder.execute(
            request_id="request-capacity",
            operation_type="model",
            operation=lambda: started.append(True),
            result_limit_bytes=512,
            error_limit_bytes=512,
        )

    assert started == []
    assert _open_manual_trace_boundary(store).list_traces() == ()


def test_known_oversized_payload_is_rejected_before_operation_starts() -> None:
    store = new_trace_store(
        capacity_bytes=4_096,
        reservation_bytes=4_096,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("known-size"),
        reservation_bytes=128,
    )
    started: list[bool] = []

    with pytest.raises(TraceCapacityError):
        recorder.execute(
            request_id="request-known-size",
            operation_type="connector",
            input_payload={"body": "x" * 2_000},
            operation=lambda: started.append(True),
            result_limit_bytes=512,
            error_limit_bytes=512,
        )

    assert started == []
    assert _open_manual_trace_boundary(store).list_traces() == ()


def test_sqlite_capacity_provider_rejects_low_physical_capacity_before_work(
    tmp_path,
) -> None:
    provider = FixedCapacityProvider(64)
    database = tmp_path / "ticket04-capacity.sqlite3"
    store = SQLiteDiagnosticTraceStore(
        database,
        capacity_bytes=32_768,
        reservation_bytes=8_192,
        capacity_provider=provider,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("sqlite-capacity"),
    )
    try:
        started: list[bool] = []
        with pytest.raises(TraceCapacityError):
            recorder.execute(
                request_id="request-low-space",
                operation_type="worker",
                operation=lambda: started.append(True),
                result_limit_bytes=512,
                error_limit_bytes=512,
            )
        assert started == []
        manual = open_sqlite_manual_trace_boundary(database)
        try:
            assert manual.list_traces(request_id="request-low-space") == ()
        finally:
            manual.close()
    finally:
        store.close()
