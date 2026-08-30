"""Durable, short-lived deduplication for inbound message identifiers."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any

DEFAULT_RETENTION = timedelta(days=7)
_UTC = UTC
_ZERO = timedelta(0)
_CACHE_LOCK = RLock()


class CacheError(RuntimeError):
    """The message-ID cache could not be read or durably updated."""


class MessageIdCache:
    """Atomically record message IDs for a bounded retention period.

    The cache intentionally has no relationship to the legacy control-plane
    state.  Its file contains only a JSON object mapping each message ID to
    the UTC timestamp at which it was claimed.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        retention: timedelta = DEFAULT_RETENTION,
    ) -> None:
        if not isinstance(path, (str, os.PathLike)):
            raise TypeError("message-ID cache path must be a path")
        if isinstance(path, str) and not path.strip():
            raise ValueError("message-ID cache path must be non-empty")
        if not isinstance(retention, timedelta):
            raise TypeError("message-ID cache retention must be a timedelta")
        if retention < _ZERO:
            raise ValueError("message-ID cache retention must not be negative")

        self.path = Path(path)
        self.retention = retention
        with _CACHE_LOCK:
            self._read_entries()

    def claim(self, message_id: str, now: datetime) -> bool:
        """Claim ``message_id`` once, returning whether this claim is new."""

        message_id = self._validate_message_id(message_id)
        now = self._validate_utc_datetime(now)

        with _CACHE_LOCK:
            entries = self._read_entries()
            active = {
                cached_id: recorded_at
                for cached_id, recorded_at in entries.items()
                if now - recorded_at < self.retention
            }
            is_new = message_id not in active
            if is_new:
                active[message_id] = now

            # Pruning is persisted even for a duplicate claim so the file
            # cannot retain entries beyond the configured retention window.
            if is_new or len(active) != len(entries):
                self._write_entries(active)
            return is_new

    @staticmethod
    def _validate_message_id(message_id: str) -> str:
        if not isinstance(message_id, str):
            raise TypeError("message ID must be a string")
        if not message_id.strip():
            raise ValueError("message ID must be non-empty")
        return message_id

    @staticmethod
    def _validate_utc_datetime(now: datetime) -> datetime:
        if not isinstance(now, datetime):
            raise TypeError("message-ID claim time must be a datetime")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("message-ID claim time must be timezone-aware UTC")
        if now.utcoffset() != _ZERO:
            raise ValueError("message-ID claim time must be timezone-aware UTC")
        return now.astimezone(_UTC)

    def _read_entries(self) -> dict[str, datetime]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream, object_pairs_hook=_strict_object)
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise CacheError("message-ID cache could not be read") from exc

        if not isinstance(payload, dict):
            raise CacheError("message-ID cache has an invalid document")

        entries: dict[str, datetime] = {}
        for message_id, recorded_at in payload.items():
            try:
                valid_id = self._validate_message_id(message_id)
                valid_timestamp = _parse_utc_timestamp(recorded_at)
            except (TypeError, ValueError) as exc:
                raise CacheError("message-ID cache has an invalid entry") from exc
            entries[valid_id] = valid_timestamp
        return entries

    def _write_entries(self, entries: dict[str, datetime]) -> None:
        document = {
            message_id: recorded_at.astimezone(_UTC).isoformat()
            for message_id, recorded_at in sorted(entries.items())
        }
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(serialized)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except (OSError, TypeError, UnicodeError, ValueError) as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
            raise CacheError("message-ID cache could not be written") from exc


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("message-ID cache contains a duplicate key")
        result[key] = value
    return result


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("message-ID cache timestamp must be a string")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("message-ID cache timestamp is not ISO-8601") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("message-ID cache timestamp must be timezone-aware UTC")
    if timestamp.utcoffset() != _ZERO:
        raise ValueError("message-ID cache timestamp must be timezone-aware UTC")
    return timestamp.astimezone(_UTC)


__all__ = ["DEFAULT_RETENTION", "CacheError", "MessageIdCache"]
