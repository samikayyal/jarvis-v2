"""Rotating verbatim JSON Lines runtime trace."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

TRACE_FAILURE_WARNING = "Warning: runtime trace could not be written; work continued."


def _system_now() -> datetime:
    return datetime.now(UTC)


class JsonlRuntimeTrace:
    """Append complete event payloads and rotate only between JSON lines."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int,
        backup_count: int,
        warning: Callable[[str], object] | None = None,
        clock: Callable[[], datetime] = _system_now,
    ) -> None:
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        if (
            not isinstance(backup_count, int)
            or isinstance(backup_count, bool)
            or backup_count <= 0
        ):
            raise ValueError("backup_count must be a positive integer")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._warning = warning or (lambda message: print(message, file=sys.stderr))
        self._clock = clock
        self._warning_pending = False
        self._lock = threading.Lock()

    def record(self, event: str, payload: dict[str, object]) -> None:
        try:
            line = self._line(event, payload)
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                current_size = self.path.stat().st_size if self.path.exists() else 0
                if current_size and current_size + len(line) > self.max_bytes:
                    self._rotate()
                with self.path.open("ab") as output:
                    output.write(line)
                    output.flush()
        except (OSError, TypeError, ValueError):
            should_warn = False
            with self._lock:
                if not self._warning_pending:
                    self._warning_pending = True
                    should_warn = True
            if should_warn:
                with suppress(Exception):
                    self._warning(TRACE_FAILURE_WARNING)

    def take_warning(self) -> str | None:
        with self._lock:
            if not self._warning_pending:
                return None
            self._warning_pending = False
            return TRACE_FAILURE_WARNING

    def _line(self, event: str, payload: dict[str, object]) -> bytes:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("trace clock must return a timezone-aware datetime")
        timestamp = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        serialized = json.dumps(
            {"timestamp": timestamp, "event": event, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (serialized + "\n").encode("utf-8")

    def _rotate(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))


__all__ = ["TRACE_FAILURE_WARNING", "JsonlRuntimeTrace"]
