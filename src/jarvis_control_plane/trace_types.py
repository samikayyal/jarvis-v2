"""Small value types shared by the trace store and write capability."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TraceReservation:
    """An in-process claim of trace capacity held until append or release."""

    reservation_id: str
    request_id: str
    reserved_bytes: int
    _owner: object = field(repr=False, compare=False)
