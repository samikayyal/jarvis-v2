"""Admission, execution, and persistence lifecycle for diagnostic traces."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

from ..models import ensure_utc
from ..ports import (
    Clock,
    DiagnosticTraceStore,
    IdGenerator,
    TraceCapacityError,
    TraceWriteError,
)
from .records import DiagnosticTrace, _validate_non_negative_int
from .values import TraceReservation, _trace_value

_T = TypeVar("_T")


class _TraceExecutionState(Enum):
    """Named lifecycle states for one admitted trace-producing operation."""

    ADMITTED = "admitted"
    STARTED = "started"
    CAPTURED = "captured"
    RETAINED = "retained"


@dataclass(slots=True)
class _TraceExecution:
    """Operation metadata plus the one reservation it is allowed to consume."""

    trace_id: str
    operation_id: str
    request_id: str
    operation_type: str
    started_at: datetime
    input_payload: Any
    arguments: Any
    telemetry: Any
    outcome: str
    reservation: TraceReservation
    state: _TraceExecutionState = _TraceExecutionState.ADMITTED

    def start(self) -> None:
        self._require(_TraceExecutionState.ADMITTED)
        self.state = _TraceExecutionState.STARTED

    def preview_size_bytes(self) -> int:
        return self._build_trace(
            completed_at=self.started_at,
            outcome=self.outcome,
            output_payload=None,
            result=None,
            error=None,
        ).serialized_size_bytes

    def capture_result(self, result: Any, completed_at: datetime) -> DiagnosticTrace:
        self._require(_TraceExecutionState.STARTED)
        trace = self._build_trace(
            completed_at=completed_at,
            outcome=self.outcome,
            output_payload=result,
            result=result,
            error=None,
        )
        self.state = _TraceExecutionState.CAPTURED
        return trace

    def capture_error(
        self, error: BaseException, completed_at: datetime
    ) -> DiagnosticTrace:
        self._require(_TraceExecutionState.STARTED)
        trace = self._build_trace(
            completed_at=completed_at,
            outcome="failed",
            output_payload=None,
            result=None,
            error=error,
        )
        self.state = _TraceExecutionState.CAPTURED
        return trace

    def mark_retained(self) -> None:
        self._require(_TraceExecutionState.CAPTURED)
        self.state = _TraceExecutionState.RETAINED

    def _build_trace(
        self,
        *,
        completed_at: datetime,
        outcome: str,
        output_payload: Any,
        result: Any,
        error: Any,
    ) -> DiagnosticTrace:
        encoded_output = _trace_value(output_payload)
        encoded_result = (
            encoded_output if output_payload is result else _trace_value(result)
        )
        return DiagnosticTrace(
            trace_id=self.trace_id,
            operation_id=self.operation_id,
            request_id=self.request_id,
            operation_type=self.operation_type,
            started_at=self.started_at,
            completed_at=ensure_utc(completed_at),
            outcome=outcome,
            payload={
                # Inputs, arguments, and telemetry were normalized before the
                # operation began.  Output, result, and error are captured only
                # after the operation boundary returns or raises.
                "input": self.input_payload,
                "output": encoded_output,
                "arguments": self.arguments,
                "result": encoded_result,
                "error": _trace_value(error),
                "telemetry": self.telemetry,
            },
        )

    def _require(self, expected: _TraceExecutionState) -> None:
        if self.state is not expected:
            raise RuntimeError(
                f"trace execution is {self.state.value}, expected {expected.value}"
            )


class _TraceAdmission:
    """Validate operation metadata and reserve capacity before work starts."""

    def __init__(
        self,
        *,
        writer: DiagnosticTraceStore,
        clock: Clock,
        ids: IdGenerator,
        reservation_bytes: int | None,
    ) -> None:
        self._writer = writer
        self._clock = clock
        self._ids = ids
        self._reservation_bytes = reservation_bytes

    def admit(
        self,
        *,
        request_id: str,
        operation_type: str,
        input_payload: Any,
        arguments: Any,
        telemetry: Any,
        operation_id: str | None,
        outcome: str,
        result_limit_bytes: int | None,
        error_limit_bytes: int | None,
    ) -> _TraceExecution:
        self._validate(
            operation_type=operation_type,
            outcome=outcome,
            result_limit_bytes=result_limit_bytes,
            error_limit_bytes=error_limit_bytes,
        )
        reservation = self._writer.reserve(
            request_id=request_id,
            reservation_bytes=self._reservation_bytes,
        )
        try:
            try:
                normalized_input = _trace_value(input_payload)
                normalized_arguments = _trace_value(arguments)
                normalized_telemetry = _trace_value(telemetry)
            except Exception as exc:
                raise TraceWriteError(
                    "trace input payload cannot be represented",
                    operation_started=False,
                ) from exc
            trace_id = self._ids.new_id("trace")
            execution = _TraceExecution(
                trace_id=trace_id,
                operation_id=operation_id or trace_id,
                request_id=request_id,
                operation_type=operation_type,
                started_at=ensure_utc(self._clock.now()),
                input_payload=normalized_input,
                arguments=normalized_arguments,
                telemetry=normalized_telemetry,
                outcome=outcome,
                reservation=reservation,
            )
            known_trace_size = execution.preview_size_bytes()
            if known_trace_size > reservation.reserved_bytes:
                raise TraceCapacityError(
                    "known trace payload exceeds its reserved capacity",
                    requested_bytes=known_trace_size,
                    available_bytes=reservation.reserved_bytes,
                )
            required_size = self._required_size(
                known_trace_size=known_trace_size,
                result_limit_bytes=result_limit_bytes,
                error_limit_bytes=error_limit_bytes,
            )
            if required_size > reservation.reserved_bytes:
                raise TraceCapacityError(
                    "declared complete trace bounds exceed reserved capacity",
                    requested_bytes=required_size,
                    available_bytes=reservation.reserved_bytes,
                )
            return execution
        except Exception:
            self._writer.release(reservation)
            raise

    @staticmethod
    def _validate(
        *,
        operation_type: str,
        outcome: str,
        result_limit_bytes: int | None,
        error_limit_bytes: int | None,
    ) -> None:
        if (
            not isinstance(operation_type, str)
            or not operation_type
            or operation_type.strip() != operation_type
        ):
            raise ValueError("operation_type must be a non-empty canonical string")
        if not isinstance(outcome, str) or not outcome or outcome.strip() != outcome:
            raise ValueError("outcome must be a non-empty canonical string")
        if result_limit_bytes is None or error_limit_bytes is None:
            raise ValueError(
                "result_limit_bytes and error_limit_bytes are required trace bounds"
            )
        _validate_non_negative_int(result_limit_bytes, "result_limit_bytes")
        _validate_non_negative_int(error_limit_bytes, "error_limit_bytes")

    @staticmethod
    def _required_size(
        *,
        known_trace_size: int,
        result_limit_bytes: int | None,
        error_limit_bytes: int | None,
    ) -> int:
        if result_limit_bytes is None or error_limit_bytes is None:
            raise AssertionError("trace bounds must be validated before sizing")
        # This budget is reserved before invoking the operation.  Once the
        # boundary has started, the actual encoded payload is authoritative:
        # it is never truncated or replaced with a trace_failed envelope.
        required_result_size = known_trace_size + 2 * (result_limit_bytes + 128)
        required_error_size = known_trace_size + error_limit_bytes + 128
        return max(required_result_size, required_error_size)


class _TracePersistence:
    """Append a captured trace and translate writer failures at one seam."""

    def __init__(self, writer: DiagnosticTraceStore) -> None:
        self._writer = writer

    def append(self, execution: _TraceExecution, trace: DiagnosticTrace) -> None:
        try:
            self._writer.append(trace, execution.reservation)
        except TraceWriteError as exc:
            raise TraceWriteError(
                str(exc) or "diagnostic trace could not be retained",
                operation_started=execution.state is not _TraceExecutionState.ADMITTED,
            ) from exc
        execution.mark_retained()


class DiagnosticTraceRecorder:
    """Reserve capacity, run one operation, and append its complete trace."""

    _READ_OPERATIONS = (
        "read_traces",
        "list_traces",
        "inspect",
        "export",
        "export_json",
        "_read_persisted_traces",
    )

    def __init__(
        self,
        *,
        writer: DiagnosticTraceStore,
        clock: Clock,
        ids: IdGenerator,
        reservation_bytes: int | None = None,
    ) -> None:
        if any(callable(getattr(writer, name, None)) for name in self._READ_OPERATIONS):
            raise TypeError(
                "DiagnosticTraceRecorder requires a write-only trace capability"
            )
        self._writer = writer
        self.clock = clock
        self.ids = ids
        self.reservation_bytes = reservation_bytes
        self._admission = _TraceAdmission(
            writer=writer,
            clock=clock,
            ids=ids,
            reservation_bytes=reservation_bytes,
        )
        self._persistence = _TracePersistence(writer)

    def execute(
        self,
        *,
        request_id: str,
        operation_type: str,
        operation: Callable[[], _T],
        input_payload: Any = None,
        arguments: Any = None,
        telemetry: Any = None,
        operation_id: str | None = None,
        outcome: str = "completed",
        result_limit_bytes: int | None = None,
        error_limit_bytes: int | None = None,
    ) -> _T:
        """Run one admitted operation and retain its complete outcome."""

        execution = self._admission.admit(
            request_id=request_id,
            operation_type=operation_type,
            input_payload=input_payload,
            arguments=arguments,
            telemetry=telemetry,
            operation_id=operation_id,
            outcome=outcome,
            result_limit_bytes=result_limit_bytes,
            error_limit_bytes=error_limit_bytes,
        )
        try:
            execution.start()
            try:
                result = operation()
            except Exception as exc:
                trace = execution.capture_error(exc, self.clock.now())
                try:
                    self._persistence.append(execution, trace)
                except TraceWriteError as write_error:
                    # The domain exception remains visible.  Persistence is
                    # its cause, never a replacement trace payload.
                    raise exc from write_error
                raise
            trace = execution.capture_result(result, self.clock.now())
            self._persistence.append(execution, trace)
            return result
        finally:
            # append() consumes the reservation.  release() is idempotent so
            # every pre-retention path gives capacity back.
            self._writer.release(execution.reservation)
