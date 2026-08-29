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


def test_trace_limit_hard_max_and_cumulative_request_budget_are_enforced() -> None:
    with pytest.raises(ValueError, match="hard_max_bytes"):
        DiagnosticTraceLimits(hard_max_bytes=MAX_TRACE_RESERVATION_BYTES + 1)

    store = new_trace_store(
        capacity_bytes=8_192,
        reservation_bytes=2_048,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("cumulative"),
        reservation_bytes=1_024,
    )
    started: list[int] = []

    for attempt in range(10):
        try:
            recorder.execute(
                request_id="request-cumulative",
                operation_type="worker",
                input_payload={"attempt": attempt},
                operation=lambda attempt=attempt: started.append(attempt),
                result_limit_bytes=64,
                error_limit_bytes=64,
            )
        except TraceCapacityError:
            break
    else:
        pytest.fail("per-request trace capacity never rejected new work")

    assert len(started) < 10
    assert len(
        _open_manual_trace_boundary(store).list_traces(request_id="request-cumulative")
    ) == len(started)
    assert store.request_retained_bytes("request-cumulative") <= 2_048


def test_recursive_result_is_retained_as_a_complete_trace_payload() -> None:
    store = new_trace_store(
        capacity_bytes=32_768,
        reservation_bytes=16_384,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("recursive"),
    )
    recursive: list[object] = []
    recursive.append(recursive)

    returned = recorder.execute(
        request_id="request-recursive",
        operation_type="model",
        operation=lambda: recursive,
        result_limit_bytes=512,
        error_limit_bytes=1_024,
    )

    assert returned is recursive
    traces = _open_manual_trace_boundary(store).list_traces(
        request_id="request-recursive"
    )
    assert len(traces) == 1
    assert traces[0].outcome == "completed"
    assert traces[0].result["__type__"] == "list"  # type: ignore[index]
    assert traces[0].result["items"][0]["__type__"] == "reference"  # type: ignore[index]
    assert traces[0].output_payload["items"][0]["__type__"] == "reference"  # type: ignore[index]


def test_oversized_result_retains_the_complete_result() -> None:
    store = new_trace_store(
        capacity_bytes=32_768,
        reservation_bytes=16_384,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("oversized-result"),
        reservation_bytes=16_384,
    )

    returned = recorder.execute(
        request_id="request-oversized-result",
        operation_type="worker",
        operation=lambda: {"body": "x" * 5_000},
        result_limit_bytes=512,
        error_limit_bytes=1_024,
    )

    assert returned == {"body": "x" * 5_000}
    traces = _open_manual_trace_boundary(store).list_traces(
        request_id="request-oversized-result"
    )
    assert len(traces) == 1
    assert traces[0].outcome == "completed"
    assert mapping_value(traces[0].result, "body") == "x" * 5_000
    assert mapping_value(traces[0].output_payload, "body") == "x" * 5_000


def test_unserializable_exception_retains_the_original_error_payload() -> None:
    class UnserializableError(RuntimeError):
        def __repr__(self) -> str:
            raise RuntimeError("repr is unavailable")

    store = new_trace_store(
        capacity_bytes=32_768,
        reservation_bytes=16_384,
    )
    recorder = DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("unserializable-error"),
        reservation_bytes=16_384,
    )

    def fail() -> None:
        error = UnserializableError(SECRET)
        error.payload = {"credential": SECRET}  # type: ignore[attr-defined]
        raise error

    with pytest.raises(UnserializableError, match=SECRET):
        recorder.execute(
            request_id="request-unserializable-error",
            operation_type="worker",
            operation=fail,
            result_limit_bytes=512,
            error_limit_bytes=64,
        )

    traces = _open_manual_trace_boundary(store).list_traces(
        request_id="request-unserializable-error"
    )
    assert len(traces) == 1
    assert traces[0].outcome == "failed"
    error_payload = traces[0].error
    assert error_payload["__type__"] == "exception"  # type: ignore[index]
    assert error_payload["message"] == SECRET  # type: ignore[index]
    attributes = error_payload["attributes"]  # type: ignore[index]
    assert mapping_value(mapping_value(attributes, "payload"), "credential") == SECRET


def test_response_reader_retries_transient_access_and_partial_acknowledgement() -> None:
    class FlakyResponsePath:
        def __init__(self) -> None:
            self.reads = 0
            self.unlinks = 0

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            self.reads += 1
            if self.reads == 1:
                raise PermissionError("response is still being published")
            if self.reads == 2:
                raise json.JSONDecodeError("partial response", "{", 1)
            return '{"ok":true}'

        def unlink(self, *, missing_ok: bool) -> None:
            assert missing_ok is True
            self.unlinks += 1
            if self.unlinks == 1:
                raise PermissionError("response is still held by the writer")

    response_path = FlakyResponsePath()
    response = _read_response_until_ready(
        response_path,  # type: ignore[arg-type]
        deadline=time.monotonic() + 1,
        operation_started=True,
    )

    assert response == {"ok": True}
    assert response_path.reads == 4
    assert response_path.unlinks == 2
