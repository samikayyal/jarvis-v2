"""Minimal configured remote-MCP prepared-tool boundary."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .runtime import ApprovalRequired, PendingAction


class McpManifestError(ValueError):
    """A configured manifest is invalid or differs from live discovery."""


class McpTransportError(RuntimeError):
    """A remote MCP request failed before a useful result was returned."""

    def __init__(self, message: str, *, kind: str = "unavailable") -> None:
        if kind not in {"unavailable", "unauthorized", "operation_failed"}:
            raise ValueError("unsupported MCP transport error kind")
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class McpServiceConfig:
    id: str
    endpoint: str
    manifest_path: Path
    max_output_chars: int

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("configured MCP service id must be non-empty")
        endpoint = urlsplit(self.endpoint)
        if endpoint.scheme != "https" or not endpoint.netloc:
            raise ValueError("configured MCP service endpoint must be an HTTPS URL")
        if not self.manifest_path.name:
            raise ValueError("configured MCP manifest path must identify a file")
        if self.max_output_chars < 2:
            raise ValueError(
                "configured MCP output limit must be at least 2 characters"
            )


@dataclass(frozen=True, slots=True)
class McpConnection:
    """Opaque identity of the one currently authorized account link."""

    id: str
    display: str

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.display.strip():
            raise ValueError("MCP connection id and display must be non-empty")
        if len(self.display) > 256:
            raise ValueError("MCP connection display must not exceed 256 characters")


@dataclass(frozen=True, slots=True)
class McpDiscovery:
    protocol_version: str
    server_info: dict[str, object]
    tools: tuple[dict[str, object], ...]


class McpTransport(Protocol):
    async def discover(self, endpoint: str, protocol_version: str) -> McpDiscovery: ...

    async def call(
        self,
        endpoint: str,
        protocol_version: str,
        operation: str,
        arguments: dict[str, object],
        connection: McpConnection,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _ManifestOperation:
    upstream: dict[str, object]
    prepared_name: str
    prepared_description: str
    input_schema: dict[str, object]
    mode: str


@dataclass(frozen=True, slots=True)
class OperationManifest:
    service_id: str
    endpoint: str
    protocol_version: str
    server_info: dict[str, object]
    captured_at: str
    operations: tuple[_ManifestOperation, ...]


@dataclass(slots=True)
class _WriteContinuation:
    connection_id: str
    operation: str
    arguments: dict[str, object]
    resolved: bool = False


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise McpManifestError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise McpManifestError(f"{name} must be a non-empty string")
    return value


def load_operation_manifest(
    source: str | Path | Mapping[str, object],
) -> OperationManifest:
    """Load the one checked-in manifest shape accepted by the MCP boundary."""

    if isinstance(source, Mapping):
        raw = copy.deepcopy(dict(source))
    else:
        path = Path(source)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise McpManifestError(f"could not read operation manifest: {exc}") from exc
    root = _object(raw, "manifest")
    if set(root) != {
        "manifest_version",
        "service",
        "captured_at",
        "operations",
    }:
        raise McpManifestError("manifest has unknown or missing fields")
    if root["manifest_version"] != 1:
        raise McpManifestError("manifest_version must be 1")
    service = _object(root["service"], "service")
    if set(service) != {"id", "endpoint", "protocol_version", "server_info"}:
        raise McpManifestError("manifest service has unknown or missing fields")
    operations_raw = root["operations"]
    if not isinstance(operations_raw, list) or not operations_raw:
        raise McpManifestError("operations must be a non-empty array")
    operations: list[_ManifestOperation] = []
    for raw_operation in operations_raw:
        operation = _object(raw_operation, "operation")
        if set(operation) != {"upstream", "prepared", "mode"}:
            raise McpManifestError("operation has unknown or missing fields")
        upstream = _object(operation["upstream"], "operation upstream")
        if not {"name", "description", "inputSchema", "annotations"} <= set(upstream):
            raise McpManifestError("upstream operation contract is incomplete")
        _text(upstream["name"], "upstream operation name")
        _text(upstream["description"], "upstream operation description")
        _object(upstream["inputSchema"], "upstream inputSchema")
        _object(upstream["annotations"], "upstream annotations")
        prepared = _object(operation["prepared"], "prepared operation")
        if set(prepared) != {"name", "description", "input_schema"}:
            raise McpManifestError("prepared operation has unknown or missing fields")
        mode = _text(operation["mode"], "operation mode")
        if mode not in {"read", "write"}:
            raise McpManifestError("operation mode must be read or write")
        input_schema = _object(prepared["input_schema"], "prepared input schema")
        _validate_schema(input_schema, "prepared input schema")
        operations.append(
            _ManifestOperation(
                upstream=upstream,
                prepared_name=_text(prepared["name"], "prepared operation name"),
                prepared_description=_text(
                    prepared["description"], "prepared operation description"
                ),
                input_schema=input_schema,
                mode=mode,
            )
        )
    names = [operation.prepared_name for operation in operations]
    upstream_names = [str(operation.upstream["name"]) for operation in operations]
    if len(names) != len(set(names)) or len(upstream_names) != len(set(upstream_names)):
        raise McpManifestError("manifest operation names must be unique")
    return OperationManifest(
        service_id=_text(service["id"], "service id"),
        endpoint=_text(service["endpoint"], "service endpoint"),
        protocol_version=_text(service["protocol_version"], "protocol version"),
        server_info=_object(service["server_info"], "server_info"),
        captured_at=_text(root["captured_at"], "captured_at"),
        operations=tuple(operations),
    )


class ConfiguredMcpService:
    """Expose only manifest-selected operations from one verified MCP service."""

    def __init__(
        self,
        config: McpServiceConfig,
        manifest: OperationManifest,
        transport: McpTransport,
    ) -> None:
        del config, manifest, transport
        raise TypeError("use ConfiguredMcpService.prepare")

    def _initialize(
        self,
        config: McpServiceConfig,
        manifest: OperationManifest,
        transport: McpTransport,
    ) -> None:
        self._config = config
        self._manifest = manifest
        self._transport = transport
        self._connection: McpConnection | None = None
        self._operations = {
            operation.prepared_name: operation for operation in manifest.operations
        }
        self.definitions = tuple(
            {
                "type": "function",
                "name": operation.prepared_name,
                "description": operation.prepared_description,
                "strict": True,
                "parameters": copy.deepcopy(operation.input_schema),
            }
            for operation in manifest.operations
        )

    @classmethod
    async def prepare(
        cls,
        config: McpServiceConfig,
        manifest: OperationManifest,
        transport: McpTransport,
    ) -> ConfiguredMcpService:
        if config.id != manifest.service_id or config.endpoint != manifest.endpoint:
            raise McpManifestError(
                "configured service identity does not match manifest"
            )
        discovery = await transport.discover(config.endpoint, manifest.protocol_version)
        discovered = {
            str(tool.get("name")): tool for tool in discovery.tools if "name" in tool
        }
        matches = (
            discovery.protocol_version == manifest.protocol_version
            and _canonical(discovery.server_info) == _canonical(manifest.server_info)
            and all(
                _canonical(discovered.get(str(operation.upstream["name"])))
                == _canonical(operation.upstream)
                for operation in manifest.operations
            )
        )
        if not matches:
            raise McpManifestError("discovery does not match manifest")
        prepared = object.__new__(cls)
        prepared._initialize(config, manifest, transport)
        return prepared

    def bind(self, connection: McpConnection | None) -> None:
        """Replace or disconnect the single current account link."""

        self._connection = connection

    async def execute(
        self, name: str, arguments: dict[str, object]
    ) -> str | ApprovalRequired:
        operation = self._operations.get(name)
        if operation is None:
            raise ValueError(f"unknown prepared tool: {name}")
        _validate_arguments(arguments, operation.input_schema)
        connection = self._connection
        if connection is None:
            return _error("not_connected")
        frozen = copy.deepcopy(arguments)
        if operation.mode == "write":
            encoded = _canonical(frozen)
            display = (
                "Run Google write?\n"
                f"Connection: {connection.display}\n"
                f"Service: {self._config.id}\n"
                f"Operation: {operation.upstream['name']}\n"
                f"Arguments: {encoded}"
            )
            continuation = _WriteContinuation(
                connection.id,
                str(operation.upstream["name"]),
                frozen,
            )
            return ApprovalRequired(
                PendingAction(
                    host="google",
                    prefix=name,
                    display=display,
                    allow_save_permission=False,
                ),
                continuation,
            )
        return await self._call(operation, frozen, connection, write=False)

    async def resume(self, continuation: object, *, approved: bool) -> str:
        if not isinstance(continuation, _WriteContinuation):
            raise TypeError("invalid MCP write continuation")
        if continuation.resolved:
            return _error("already_resolved")
        continuation.resolved = True
        if not approved:
            return _canonical({"rejected": True})
        connection = self._connection
        if connection is None or connection.id != continuation.connection_id:
            return _error("connection_changed")
        operation = next(
            item
            for item in self._manifest.operations
            if item.upstream["name"] == continuation.operation
        )
        return await self._call(
            operation, continuation.arguments, connection, write=True
        )

    async def _call(
        self,
        operation: _ManifestOperation,
        arguments: dict[str, object],
        connection: McpConnection,
        *,
        write: bool,
    ) -> str:
        try:
            result = await self._transport.call(
                self._config.endpoint,
                self._manifest.protocol_version,
                str(operation.upstream["name"]),
                copy.deepcopy(arguments),
                connection,
            )
        except McpTransportError as exc:
            return _error("outcome_ambiguous" if write else exc.kind)
        except Exception:  # noqa: BLE001 - remote boundary failures are normalized
            return _error("outcome_ambiguous" if write else "unavailable")
        try:
            encoded = _canonical({"result": result})
        except (TypeError, ValueError, OverflowError):
            return _error("outcome_ambiguous" if write else "invalid_response")
        if len(encoded) > self._config.max_output_chars:
            return _error("output_too_large")
        return encoded


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _error(kind: str) -> str:
    return _canonical({"error": {"kind": kind}})


def _validate_schema(schema: dict[str, object], name: str) -> None:
    expected = schema.get("type")
    allowed = {"string", "integer", "number", "boolean", "object", "array"}
    if expected not in allowed:
        raise McpManifestError(f"{name} has an unsupported type")
    enum = schema.get("enum")
    if enum is not None and (
        not isinstance(enum, list)
        or not enum
        or any(not _matches_type(item, str(expected)) for item in enum)
    ):
        raise McpManifestError(f"{name} enum must be a non-empty typed array")
    common = {"type", "description", "enum"}
    fields = {
        "string": {"minLength", "maxLength"},
        "integer": {"minimum", "maximum"},
        "number": {"minimum", "maximum"},
        "boolean": set(),
        "object": {"properties", "required", "additionalProperties"},
        "array": {"items", "minItems", "maxItems"},
    }[str(expected)]
    if not set(schema) <= common | fields:
        raise McpManifestError(f"{name} contains unsupported schema keywords")
    if expected == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
            or not set(required) <= set(properties)
            or schema.get("additionalProperties") is not False
        ):
            raise McpManifestError(f"{name} must be a closed object schema")
        for field_name, field_schema in properties.items():
            if not isinstance(field_name, str) or not isinstance(field_schema, dict):
                raise McpManifestError(f"{name} properties must be schemas")
            _validate_schema(field_schema, f"{name}.{field_name}")
    elif expected == "array":
        items = schema.get("items")
        if not isinstance(items, dict) or not isinstance(schema.get("maxItems"), int):
            raise McpManifestError(f"{name} array must have items and maxItems")
        _validate_schema(items, f"{name} items")
    elif expected == "string" and not isinstance(schema.get("maxLength"), int):
        raise McpManifestError(f"{name} string must have maxLength")


def _validate_arguments(arguments: object, schema: dict[str, object]) -> None:
    _validate_value(arguments, schema, "arguments")


def _matches_type(value: object, expected: str) -> bool:
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }[expected]


def _validate_object(
    value: dict[object, object], schema: dict[str, object], name: str
) -> None:
    properties = schema.get("properties")
    required = schema.get("required")
    assert isinstance(properties, dict) and isinstance(required, list)
    missing = set(required) - set(value)
    if missing:
        raise ValueError(f"missing required prepared tool arguments: {sorted(missing)}")
    extra = set(value) - set(properties)
    if extra:
        raise ValueError(f"unknown prepared tool arguments: {sorted(extra)}")
    for field_name, field_value in value.items():
        field_schema = properties.get(field_name)
        assert isinstance(field_schema, dict)
        _validate_value(field_value, field_schema, f"{name}.{field_name}")


def _validate_value(value: object, schema: dict[str, object], name: str) -> None:
    expected = schema.get("type")
    if isinstance(expected, str) and not _matches_type(value, expected):
        raise ValueError(f"prepared tool argument {name} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:  # type: ignore[operator]
        raise ValueError(f"prepared tool argument {name} is outside its allowlist")
    if isinstance(value, str):
        if (
            isinstance(schema.get("minLength"), int)
            and len(value) < schema["minLength"]
        ):  # type: ignore[operator]
            raise ValueError(f"prepared tool argument {name} is too short")
        if (
            isinstance(schema.get("maxLength"), int)
            and len(value) > schema["maxLength"]
        ):  # type: ignore[operator]
            raise ValueError(f"prepared tool argument {name} is too long")
    if isinstance(value, int) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), int) and value < schema["minimum"]:  # type: ignore[operator]
            raise ValueError(f"prepared tool argument {name} is below its bound")
        if isinstance(schema.get("maximum"), int) and value > schema["maximum"]:  # type: ignore[operator]
            raise ValueError(f"prepared tool argument {name} exceeds its bound")
    if isinstance(value, dict) and expected == "object":
        _validate_object(value, schema, name)
    if isinstance(value, list) and expected == "array":
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"prepared tool argument {name} has too few items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"prepared tool argument {name} exceeds its item bound")
        items = schema.get("items")
        assert isinstance(items, dict)
        for index, item in enumerate(value):
            _validate_value(item, items, f"{name}[{index}]")


__all__ = [
    "ConfiguredMcpService",
    "McpConnection",
    "McpDiscovery",
    "McpManifestError",
    "McpServiceConfig",
    "McpTransport",
    "McpTransportError",
    "OperationManifest",
    "load_operation_manifest",
]
