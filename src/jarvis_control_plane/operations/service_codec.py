"""Canonical authenticated service-frame encoding and verification."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import importlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

from .. import models, ports
from .service_protocol import ServiceAuthenticationError, ServiceProtocolError

MAX_FRAME_BYTES = 16_777_216
MAX_REQUEST_FRAME_BYTES = 1_048_576


def _type_registry() -> Mapping[str, type[Any]]:
    registry: dict[str, type[Any]] = {}
    root_package = __package__.rpartition(".")[0]
    approved_modules = (models, ports) + tuple(
        importlib.import_module(f"{root_package}.{name}")
        for name in (
            "google_oauth",
            "google_reads",
            "knowledge_vault",
            "knowledge_vault_writes",
            "openwa",
            "sessions",
            "terminal_policy",
            "worker_gateway",
        )
    )
    for module in approved_modules:
        for value in vars(module).values():
            if isinstance(value, type) and (
                dataclasses.is_dataclass(value) or issubclass(value, Enum)
            ):
                registry[f"{value.__module__}.{value.__qualname__}"] = value
    return MappingProxyType(registry)


_TYPES = _type_registry()


def _encode(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("service protocol floats must be finite")
        return value
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, Enum):
        return {
            "$enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _encode(value.value),
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        type_name = f"{type(value).__module__}.{type(value).__qualname__}"
        if type_name not in _TYPES:
            raise TypeError(f"type is outside the service protocol: {type_name}")
        return {
            "$type": type_name,
            "fields": {
                field.name: _encode(getattr(value, field.name))
                for field in dataclasses.fields(value)
            },
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("service protocol mappings require string keys")
        return {"$mapping": {key: _encode(item) for key, item in value.items()}}
    if isinstance(value, (tuple, list)):
        return {"$sequence": [_encode(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        encoded = [_encode(item) for item in value]
        return {"$set": sorted(encoded, key=lambda item: repr(item))}
    raise TypeError(f"value is outside the service protocol: {type(value).__name__}")


def _decode(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ServiceProtocolError("service protocol floats must be finite")
        return value
    if not isinstance(value, dict):
        raise ServiceProtocolError("invalid typed protocol value")
    if "$enum" in value:
        if set(value) != {"$enum", "value"}:
            raise ServiceProtocolError("invalid encoded enum")
        enum_type = _TYPES.get(value["$enum"])
        if enum_type is None or not issubclass(enum_type, Enum):
            raise ServiceProtocolError("encoded enum is outside the registry")
        return enum_type(_decode(value["value"]))
    if "$type" in value:
        if set(value) != {"$type", "fields"}:
            raise ServiceProtocolError("invalid encoded model")
    elif len(value) != 1:
        raise ServiceProtocolError("invalid typed protocol value")
    if "$bytes" in value:
        try:
            return base64.b64decode(value["$bytes"], validate=True)
        except (TypeError, ValueError) as exc:
            raise ServiceProtocolError("invalid encoded bytes") from exc
    if "$datetime" in value:
        try:
            return datetime.fromisoformat(value["$datetime"])
        except (TypeError, ValueError) as exc:
            raise ServiceProtocolError("invalid encoded datetime") from exc
    if "$decimal" in value:
        try:
            return Decimal(value["$decimal"])
        except (TypeError, InvalidOperation) as exc:
            raise ServiceProtocolError("invalid encoded decimal") from exc
    if "$sequence" in value:
        items = value["$sequence"]
        if not isinstance(items, list):
            raise ServiceProtocolError("invalid encoded sequence")
        return tuple(_decode(item) for item in items)
    if "$set" in value:
        items = value["$set"]
        if not isinstance(items, list):
            raise ServiceProtocolError("invalid encoded set")
        return frozenset(_decode(item) for item in items)
    if "$mapping" in value:
        items = value["$mapping"]
        if not isinstance(items, dict) or not all(isinstance(k, str) for k in items):
            raise ServiceProtocolError("invalid encoded mapping")
        return {key: _decode(item) for key, item in items.items()}
    type_name = value.get("$type")
    fields = value.get("fields")
    model_type = _TYPES.get(type_name)
    if model_type is None or not dataclasses.is_dataclass(model_type):
        raise ServiceProtocolError("encoded type is outside the registry")
    if not isinstance(fields, dict):
        raise ServiceProtocolError("invalid encoded model fields")
    allowed_fields = {field.name for field in dataclasses.fields(model_type)}
    if set(fields) != allowed_fields:
        raise ServiceProtocolError("encoded model fields do not match the registry")
    return model_type(**{name: _decode(item) for name, item in fields.items()})


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sign(frame: Mapping[str, object], secret: bytes) -> str:
    return hmac.new(secret, _canonical_json(frame), hashlib.sha256).hexdigest()


def _signed_frame(
    frame: dict[str, object], secret: bytes, *, max_bytes: int = MAX_FRAME_BYTES
) -> bytes:
    signed = {**frame, "signature": _sign(frame, secret)}
    payload = _canonical_json(signed)
    if len(payload) > max_bytes:
        raise ServiceProtocolError("service protocol frame exceeds its fixed bound")
    return payload


def _verify_frame(
    payload: bytes, secret: bytes, *, max_bytes: int = MAX_FRAME_BYTES
) -> dict[str, object]:
    if len(payload) > max_bytes:
        raise ServiceProtocolError("service protocol frame exceeds its fixed bound")
    try:
        frame = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceProtocolError("service protocol frame is not valid JSON") from exc
    if not isinstance(frame, dict):
        raise ServiceProtocolError("service protocol frame must be an object")
    signature = frame.pop("signature", None)
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature, _sign(frame, secret)
    ):
        raise ServiceAuthenticationError("service protocol authentication failed")
    return frame


def _peek_client_identity(payload: bytes) -> str:
    """Select a per-link key without treating the unverified identity as trusted."""

    if len(payload) > MAX_REQUEST_FRAME_BYTES:
        raise ServiceProtocolError("service protocol frame exceeds its fixed bound")
    try:
        frame = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceProtocolError("service protocol frame is not valid JSON") from exc
    identity = frame.get("client_identity") if isinstance(frame, dict) else None
    if not isinstance(identity, str) or not identity:
        raise ServiceAuthenticationError("service client identity is invalid")
    return identity
