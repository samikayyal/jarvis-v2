"""JSON-safe graph encoding for complete diagnostic trace payloads."""

from __future__ import annotations

import base64
import json
import math
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field, is_dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..models import ensure_utc


@dataclass(frozen=True, slots=True)
class TraceReservation:
    """An in-process claim of trace capacity held until append or release."""

    reservation_id: str
    request_id: str
    reserved_bytes: int
    _owner: object = field(repr=False, compare=False)


def _safe_text(value: Any, *, fallback: str) -> str:
    try:
        return str(value)
    except BaseException:  # noqa: BLE001 - trace capture must survive hostile values
        return fallback


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except BaseException as exc:  # noqa: BLE001 - trace capture must never call user code twice
        error_type = f"{type(exc).__module__}.{type(exc).__qualname__}"
        return (
            f"<repr failed: {error_type}: {_safe_text(exc, fallback='unknown error')}>"
        )


class _TraceValueEncoder:
    """Encode one object graph without dropping cycles or unexpected values.

    The old recursive converter treated an object graph as a tree and raised
    on the first cycle or unsupported adapter value.  A diagnostic trace is
    a record of what happened, so the encoder assigns identities to graph
    nodes and uses explicit references for cycles.  Values that do not have
    a built-in lossless JSON representation are retained as a structural
    object snapshot, including attributes and a safe representation.
    """

    __slots__ = ("_active", "_next_reference", "_references")

    def __init__(self) -> None:
        self._active: set[int] = set()
        self._next_reference = 1
        self._references: dict[int, int] = {}

    def encode(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            return {"__type__": "float", "value": _safe_repr(value)}
        if isinstance(value, datetime):
            return {
                "__type__": "datetime",
                "value": ensure_utc(value).isoformat(),
            }
        if isinstance(value, bytes):
            return {
                "__type__": "bytes",
                "base64": base64.b64encode(value).decode("ascii"),
            }
        if isinstance(value, bytearray):
            return {
                "__type__": "bytearray",
                "base64": base64.b64encode(bytes(value)).decode("ascii"),
            }
        if isinstance(value, memoryview):
            return {
                "__type__": "memoryview",
                "base64": base64.b64encode(value.tobytes()).decode("ascii"),
            }
        if isinstance(value, Path):
            return {"__type__": "path", "value": str(value)}
        if isinstance(value, Enum):
            return {
                "__type__": "enum",
                "class": self._class_name(value),
                "name": value.name,
                "value": self.encode(value.value),
            }

        reference = self._begin_node(value)
        if isinstance(reference, dict):
            return reference
        try:
            if isinstance(value, BaseException):
                return self._exception(value, reference)
            if isinstance(value, Mapping):
                return self._mapping(value, reference)
            if is_dataclass(value) and not isinstance(value, type):
                fields: dict[str, Any] = {}
                for item in dataclass_fields(value):
                    try:
                        fields[item.name] = getattr(value, item.name)
                    except BaseException as exc:  # noqa: BLE001 - preserve field access failure
                        fields[item.name] = self._attribute_error(exc)
                return self._identified(
                    {
                        "__type__": "dataclass",
                        "class": self._class_name(value),
                        "fields": self.encode(fields),
                    },
                    reference,
                )
            if isinstance(value, (list, tuple, set, frozenset)):
                return self._sequence(value, reference)
            return self._object(value, reference)
        finally:
            self._active.discard(id(value))

    def _begin_node(self, value: Any) -> int | dict[str, int]:
        identity = id(value)
        if identity in self._active:
            existing = self._references[identity]
            return {"__type__": "reference", "id": existing}
        reference = self._next_reference
        self._next_reference += 1
        self._references[identity] = reference
        self._active.add(identity)
        return reference

    @staticmethod
    def _class_name(value: Any) -> str:
        value_type = type(value)
        return f"{value_type.__module__}.{value_type.__qualname__}"

    @staticmethod
    def _identified(value: dict[str, Any], reference: int) -> dict[str, Any]:
        value["id"] = reference
        return value

    def _mapping(self, value: Mapping[Any, Any], reference: int) -> dict[str, Any]:
        return self._identified(
            {
                "__type__": "mapping",
                "items": [
                    {"key": self.encode(key), "value": self.encode(item)}
                    for key, item in value.items()
                ],
            },
            reference,
        )

    def _sequence(self, value: Any, reference: int) -> dict[str, Any]:
        values = [self.encode(item) for item in value]
        if isinstance(value, set | frozenset):
            values.sort(key=_canonical_json)
            sequence_type = "frozenset" if isinstance(value, frozenset) else "set"
        elif isinstance(value, list):
            sequence_type = "list"
        else:
            sequence_type = "tuple"
        return self._identified(
            {"__type__": sequence_type, "items": values},
            reference,
        )

    def _exception(self, value: BaseException, reference: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "__type__": "exception",
            "class": self._class_name(value),
            "message": _safe_text(value, fallback="<str failed>"),
            "args": self.encode(value.args),
            "repr": _safe_repr(value),
            "attributes": self.encode(self._attributes(value)),
            "traceback": self._traceback(value),
            "suppress_context": bool(value.__suppress_context__),
        }
        if value.__cause__ is not None:
            payload["cause"] = self.encode(value.__cause__)
        if value.__context__ is not None:
            payload["context"] = self.encode(value.__context__)
        notes = getattr(value, "__notes__", None)
        if notes is not None:
            payload["notes"] = self.encode(notes)
        return self._identified(payload, reference)

    @staticmethod
    def _traceback(value: BaseException) -> list[str]:
        try:
            return traceback.format_exception(type(value), value, value.__traceback__)
        except BaseException:  # noqa: BLE001 - formatting is diagnostic best effort
            return ["<traceback unavailable>"]

    def _object(self, value: Any, reference: int) -> dict[str, Any]:
        return self._identified(
            {
                "__type__": "object",
                "class": self._class_name(value),
                "attributes": self.encode(self._attributes(value)),
                "repr": _safe_repr(value),
            },
            reference,
        )

    @staticmethod
    def _attribute_error(exc: BaseException) -> dict[str, str]:
        return {
            "__type__": "attribute_error",
            "class": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": _safe_text(exc, fallback="<str failed>"),
        }

    @staticmethod
    def _attributes(value: Any) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        try:
            attributes.update(vars(value))
        except (TypeError, AttributeError):
            pass
        for value_type in type(value).__mro__:
            slots = value_type.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for name in slots:
                if name in {"__dict__", "__weakref__"} or name in attributes:
                    continue
                try:
                    attributes[name] = getattr(value, name)
                except AttributeError:
                    continue
                except BaseException as exc:  # noqa: BLE001 - preserve hostile slot access
                    attributes[name] = _TraceValueEncoder._attribute_error(exc)
        return attributes


def _trace_value(value: Any) -> Any:
    """Convert one complete operation value into a JSON-safe graph snapshot."""

    return _TraceValueEncoder().encode(value)


def _freeze_trace_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_trace_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_trace_value(item) for item in value)
    return value


def _thaw_trace_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_trace_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_trace_value(item) for item in value]
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
