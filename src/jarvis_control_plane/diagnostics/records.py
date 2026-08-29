"""Immutable diagnostic trace records and capacity limits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..models import ensure_utc
from .values import _canonical_json, _freeze_trace_value, _thaw_trace_value

DEFAULT_TRACE_RESERVATION_BYTES = 16 * 1024 * 1024
MAX_TRACE_RESERVATION_BYTES = 64 * 1024 * 1024


def _validate_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _validate_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class DiagnosticTraceLimits:
    """Configured full-trace reservation and store capacity.

    ``reservation_bytes`` is the conservative per-operation reservation.  It
    may be lowered for a controlled environment but may not exceed the V1 hard
    maximum.  ``capacity_bytes`` defaults to four reservations so a normal
    request can retain several sequential operation spans.  It is intentionally
    not relieved by deleting old traces.
    """

    reservation_bytes: int = DEFAULT_TRACE_RESERVATION_BYTES
    hard_max_bytes: int = MAX_TRACE_RESERVATION_BYTES
    capacity_bytes: int | None = None

    def __post_init__(self) -> None:
        reservation = _validate_positive_int(
            self.reservation_bytes, "reservation_bytes"
        )
        hard_max = _validate_positive_int(self.hard_max_bytes, "hard_max_bytes")
        if reservation > hard_max:
            raise ValueError("reservation_bytes cannot exceed hard_max_bytes")
        if hard_max > MAX_TRACE_RESERVATION_BYTES:
            raise ValueError("hard_max_bytes cannot exceed the V1 hard maximum")
        capacity = self.capacity_bytes
        if capacity is None:
            capacity = reservation * 4
        _validate_positive_int(capacity, "capacity_bytes")
        object.__setattr__(self, "capacity_bytes", capacity)


@dataclass(frozen=True, slots=True)
class DiagnosticTrace:
    """One complete, permanent operation trace.

    The payload always contains ``input``, ``output``, ``arguments``,
    ``result``, ``error``, and ``telemetry`` keys.  No field is redacted or
    dropped based on its name or apparent sensitivity.
    """

    trace_id: str
    operation_id: str
    request_id: str
    operation_type: str
    started_at: datetime
    completed_at: datetime
    outcome: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "trace_id",
            "operation_id",
            "request_id",
            "operation_type",
            "outcome",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ValueError(f"{name} must be a non-empty canonical string")
        if not isinstance(self.payload, Mapping):
            raise TypeError("trace payload must be a mapping")
        required_payload = {
            "input",
            "output",
            "arguments",
            "result",
            "error",
            "telemetry",
        }
        if not required_payload.issubset(self.payload):
            raise ValueError("trace payload is missing a complete operation field")
        object.__setattr__(self, "started_at", ensure_utc(self.started_at))
        object.__setattr__(self, "completed_at", ensure_utc(self.completed_at))
        object.__setattr__(self, "payload", _freeze_trace_value(self.payload))

    @property
    def span_type(self) -> str:
        """Alias matching the domain glossary's span terminology."""

        return self.operation_type

    @property
    def input_payload(self) -> Any:
        return self.payload.get("input")

    @property
    def output_payload(self) -> Any:
        return self.payload.get("output")

    @property
    def arguments(self) -> Any:
        return self.payload.get("arguments")

    @property
    def result(self) -> Any:
        return self.payload.get("result")

    @property
    def error(self) -> Any:
        return self.payload.get("error")

    @property
    def telemetry(self) -> Any:
        return self.payload.get("telemetry")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "operation_id": self.operation_id,
            "request_id": self.request_id,
            "operation_type": self.operation_type,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "outcome": self.outcome,
            "payload": _thaw_trace_value(self.payload),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DiagnosticTrace:
        if not isinstance(value, Mapping):
            raise TypeError("trace record must be a mapping")
        return cls(
            trace_id=value["trace_id"],
            operation_id=value["operation_id"],
            request_id=value["request_id"],
            operation_type=value["operation_type"],
            started_at=datetime.fromisoformat(value["started_at"]),
            completed_at=datetime.fromisoformat(value["completed_at"]),
            outcome=value["outcome"],
            payload=value["payload"],
        )

    @property
    def serialized_size_bytes(self) -> int:
        return len(_canonical_json(self.to_mapping()))
