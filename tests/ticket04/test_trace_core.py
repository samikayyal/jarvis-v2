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


def test_trace_retains_complete_payloads_only_through_manual_boundary() -> None:
    store = new_trace_store(
        capacity_bytes=32_768,
        reservation_bytes=8_192,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("trace"),
    )

    returned = recorder.execute(
        request_id="request-004",
        operation_id="request-004:model",
        operation_type="model",
        input_payload={"prompt": SECRET},
        arguments={"api_key": SECRET},
        telemetry={"private_note": SECRET},
        operation=lambda: {"output": SECRET, "bytes": b"raw"},
        result_limit_bytes=2_048,
        error_limit_bytes=2_048,
    )

    assert returned["output"] == SECRET
    assert not hasattr(recorder, "store")
    assert not hasattr(recorder._writer, "_store")
    assert not hasattr(recorder._writer, "_append_fn")
    assert not hasattr(recorder._writer, "_thread")
    assert not hasattr(recorder._writer, "_connection")
    assert not hasattr(recorder._writer, "_request")
    assert not hasattr(recorder._writer, "_read_persisted_traces")
    assert {
        name
        for name in dir(recorder._writer)
        if not name.startswith("_") and callable(getattr(recorder._writer, name))
    } == {"append", "release", "reserve"}
    for operation_name in ("append", "release", "reserve"):
        operation = getattr(recorder._writer, operation_name)
        operation_globals = operation.__func__.__globals__
        assert "Client" not in operation_globals
        assert "_WRITER_ENDPOINTS" not in operation_globals
        assert "_writer_authkey" not in operation_globals
        assert "_writer_endpoint" not in operation_globals
        assert "_writer_request" not in operation_globals
        reachable = bounded_reachable(operation)
        assert not any(isinstance(value, Connection) for value in reachable)
        assert not any(isinstance(value, BaseProcess) for value in reachable)
        assert not any(
            isinstance(value, _DiagnosticTraceStoreBase) for value in reachable
        )
        assert not any(
            isinstance(value, ManualDiagnosticTraceBoundary) for value in reachable
        )
        assert SECRET not in reachable
    assert not hasattr(store, "records")
    assert not hasattr(store, "list_traces")
    manual = _open_manual_trace_boundary(store)
    traces = manual.list_traces(request_id="request-004")
    assert len(traces) == 1
    trace = traces[0]
    assert trace.operation_type == "model"
    assert mapping_value(trace.input_payload, "prompt") == SECRET
    assert mapping_value(trace.arguments, "api_key") == SECRET
    assert mapping_value(trace.output_payload, "output") == SECRET
    assert mapping_value(trace.telemetry, "private_note") == SECRET
    assert mapping_value(trace.output_payload, "bytes") == {
        "__type__": "bytes",
        "base64": "cmF3",
    }
    assert SECRET in manual.export_json(request_id="request-004").decode()
    assert not hasattr(store, "_manual_access_token")
    assert not hasattr(store, "_read_for_manual_admin")
    with pytest.raises(AttributeError):
        ManualDiagnosticTraceBoundary(store).list_traces()


def test_recorder_rejects_a_readable_store_as_a_control_plane_capability() -> None:
    store = new_trace_store()

    with pytest.raises(TypeError, match="write-only"):
        DiagnosticTraceRecorder(
            writer=store,
            clock=FixedClock(NOW),
            ids=DeterministicIdGenerator("readable-store"),
        )


def test_writer_finalization_stops_child_with_admin_channel_open() -> None:
    store = new_trace_store(
        capacity_bytes=32_768,
        reservation_bytes=8_192,
    )
    writer = store.writer()
    process = store._service_process
    assert process is not None

    del writer
    gc.collect()
    process.join(timeout=2)
    try:
        assert not process.is_alive()
    finally:
        store._close_writer_service()


def test_failed_operation_retains_complete_error_and_does_not_expire() -> None:
    clock = FixedClock(NOW)
    store = new_trace_store(
        capacity_bytes=32_768,
        reservation_bytes=8_192,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=clock,
        ids=DeterministicIdGenerator("trace-error"),
    )

    def fail() -> None:
        raise RuntimeError(SECRET)

    with pytest.raises(RuntimeError, match=SECRET):
        recorder.execute(
            request_id="request-error",
            operation_type="worker",
            input_payload={"input": SECRET},
            arguments={"command": "controlled"},
            operation=fail,
            result_limit_bytes=2_048,
            error_limit_bytes=2_048,
        )

    clock.advance(minutes=365 * 10)
    traces = _open_manual_trace_boundary(store).list_traces(request_id="request-error")
    assert len(traces) == 1
    assert traces[0].outcome == "failed"
    assert traces[0].error["message"] == SECRET


def test_non_json_payload_types_are_explicitly_tagged_without_collisions() -> None:
    store = new_trace_store(
        capacity_bytes=32_768,
        reservation_bytes=8_192,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("typed"),
    )
    recorder.execute(
        request_id="request-typed",
        operation_type="model",
        input_payload={
            "dataclass": TypedTracePayload(
                when=NOW,
                path=Path("C:/private/payload"),
                values=("one",),
            ),
            "tuple": ("one",),
            "list": ["one"],
            "set": {"one"},
            "frozenset": frozenset({"one"}),
            "enum": TraceMode.LIVE,
            "datetime": NOW,
            "path": Path("C:/private/payload"),
            "literal_tag_like_mapping": {
                "__type__": "bytes",
                "base64": "not-a-bytes-envelope",
            },
        },
        operation=lambda: None,
        result_limit_bytes=512,
        error_limit_bytes=2_048,
    )

    payload = _open_manual_trace_boundary(store).list_traces()[0].input_payload
    dataclass_payload = mapping_value(payload, "dataclass")
    dataclass_fields = dataclass_payload["fields"]  # type: ignore[index]
    assert dataclass_payload["__type__"] == "dataclass"  # type: ignore[index]
    assert dataclass_fields["__type__"] == "mapping"  # type: ignore[index]
    assert mapping_value(dataclass_fields, "when")["__type__"] == "datetime"  # type: ignore[index]
    assert mapping_value(dataclass_fields, "path")["__type__"] == "path"  # type: ignore[index]
    assert mapping_value(dataclass_fields, "values")["__type__"] == "tuple"  # type: ignore[index]
    assert mapping_value(payload, "tuple")["__type__"] == "tuple"  # type: ignore[index]
    assert mapping_value(payload, "list")["__type__"] == "list"  # type: ignore[index]
    assert mapping_value(payload, "set")["__type__"] == "set"  # type: ignore[index]
    assert mapping_value(payload, "frozenset")["__type__"] == "frozenset"  # type: ignore[index]
    assert mapping_value(payload, "enum")["class"].endswith("TraceMode")  # type: ignore[index]
    assert mapping_value(payload, "datetime")["__type__"] == "datetime"  # type: ignore[index]
    assert mapping_value(payload, "path")["__type__"] == "path"  # type: ignore[index]
    literal_mapping = mapping_value(payload, "literal_tag_like_mapping")
    assert literal_mapping["__type__"] == "mapping"  # type: ignore[index]
    assert mapping_value(literal_mapping, "__type__") == "bytes"


def test_sqlite_trace_store_survives_reconstruction_without_expiry(tmp_path) -> None:
    database = tmp_path / "ticket04-traces.sqlite3"
    store = SQLiteDiagnosticTraceStore(
        database,
        capacity_bytes=32_768,
        reservation_bytes=8_192,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("sqlite-trace"),
    )
    recorder.execute(
        request_id="request-sqlite",
        operation_type="connector",
        input_payload={"token": SECRET},
        operation=lambda: {"result": SECRET},
        result_limit_bytes=2_048,
        error_limit_bytes=2_048,
    )
    store.close()

    reconstructed = SQLiteDiagnosticTraceStore(
        database,
        capacity_bytes=32_768,
        reservation_bytes=8_192,
    )
    try:
        manual = open_sqlite_manual_trace_boundary(database)
        try:
            traces = manual.list_traces(request_id="request-sqlite")
            assert len(traces) == 1
            assert mapping_value(traces[0].input_payload, "token") == SECRET
            assert mapping_value(traces[0].result, "result") == SECRET
        finally:
            manual.close()
    finally:
        reconstructed.close()
