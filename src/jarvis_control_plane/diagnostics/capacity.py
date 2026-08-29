"""Physical-capacity providers used by durable diagnostic trace stores."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Protocol

from ..ports import TraceCapacityError
from .records import _validate_non_negative_int


class _TraceCapacityProvider(Protocol):  # noqa: PYI046 - consumed by store.py
    """Storage-side capacity reservation used by durable trace stores."""

    def available_bytes(self) -> int: ...

    def reserve(self, amount: int) -> None: ...

    def release(self, amount: int) -> None: ...


class _FileSystemTraceCapacityProvider:
    """Conservatively account for free space and concurrent local reservations."""

    def __init__(self, location: str | Path, *, minimum_free_bytes: int = 0) -> None:
        location_path = Path(location)
        self._directory = (
            Path.cwd()
            if str(location_path) == ":memory:"
            else location_path.expanduser().resolve().parent
        )
        self._reserved = 0
        self._minimum_free_bytes = _validate_non_negative_int(
            minimum_free_bytes, "minimum_free_bytes"
        )
        self._lock = threading.RLock()

    def available_bytes(self) -> int:
        with self._lock:
            try:
                free = shutil.disk_usage(self._directory).free
            except OSError:
                return 0
            return max(0, free - self._minimum_free_bytes - self._reserved)

    def reserve(self, amount: int) -> None:
        with self._lock:
            available = self.available_bytes()
            if amount > available:
                raise TraceCapacityError(
                    "filesystem trace capacity is insufficient",
                    requested_bytes=amount,
                    available_bytes=available,
                )
            self._reserved += amount

    def release(self, amount: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - amount)


class _StaticTraceCapacityProvider:
    """Serializable capacity snapshot used by a child trace-writer process."""

    def __init__(self, available: int) -> None:
        self._available = available

    def available_bytes(self) -> int:
        return self._available

    def reserve(self, amount: int) -> None:
        if amount > self._available:
            raise TraceCapacityError(
                "trace capacity provider is insufficient",
                requested_bytes=amount,
                available_bytes=self._available,
            )
        self._available -= amount

    def release(self, amount: int) -> None:
        self._available += amount
