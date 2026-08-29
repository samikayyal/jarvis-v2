"""Configuration, identity, and secret helpers for service composition."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..gmail_actions import GMAIL_SEND_SCOPE
from ..worker_gateway import WorkerExecutionLimits
from .deployment import BundleValidationError, validate_configuration


@dataclass(frozen=True, slots=True)
class ServiceRole:
    name: str
    identity: str
    port: int
    operations: tuple[str, ...]


SERVICE_ROLES: Mapping[str, ServiceRole] = {
    role.name: role
    for role in (
        ServiceRole("inbound_receiver", "jarvis-inbound", 9011, ("receive",)),
        ServiceRole("capability_broker", "jarvis-broker", 9012, ("receive",)),
        ServiceRole(
            "orchestration_agent", "jarvis-orchestration", 9013, ("run", "cancel")
        ),
        ServiceRole(
            "audit_service",
            "jarvis-audit",
            9014,
            ("append", "append_batch", "writable", "safe_view", "export_json"),
        ),
        ServiceRole(
            "google_connector",
            "jarvis-google",
            9015,
            (
                "gmail_messages_list",
                "gmail_messages_get",
                "gmail_threads_list",
                "gmail_threads_get",
                "drive_files_list",
                "drive_files_get",
                "drive_files_export",
                "current",
                "current_connection_generation",
                "start_authorization",
                "oauth_callback",
                "disconnect",
                "action_bind",
                "action_validate",
                "action_prepare",
                "action_run",
                "action_cancel",
                "action_finalize",
            ),
        ),
        ServiceRole(
            "knowledge_vault_connector",
            "jarvis-vault",
            9016,
            (
                "read",
                "propose",
                "action_bind",
                "action_validate",
                "action_prepare",
                "action_run",
                "action_cancel",
                "action_finalize",
            ),
        ),
        ServiceRole(
            "openwa_outbound_connector",
            "jarvis-openwa-outbound",
            9017,
            ("current", "preflight", "send"),
        ),
        ServiceRole(
            "worker_gateway",
            "jarvis-worker-gateway",
            9018,
            (
                "current",
                "action_prepare",
                "action_run",
                "action_cancel",
                "action_finalize",
            ),
        ),
        ServiceRole(
            "public_oauth_callback", "jarvis-oauth-callback", 8080, ("callback",)
        ),
        ServiceRole(
            "deleted_conversation_archive",
            "jarvis-deleted-archive",
            0,
            (),
        ),
    )
}

_GOOGLE_AUTHORIZATION_ACCESS_SCOPES: Mapping[str, frozenset[str]] = {
    "baseline": frozenset(),
    "gmail-send": frozenset({GMAIL_SEND_SCOPE}),
}


class CompositionError(RuntimeError):
    """A production role could not be assembled from reviewed configuration."""


def load_configuration(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    if not isinstance(value, dict):
        raise CompositionError("deployment configuration must be a TOML table")
    try:
        validate_configuration(value)
    except BundleValidationError as exc:
        raise CompositionError("active configuration failed validation") from exc
    if value.get("configuration_kind") != "active":
        raise CompositionError("service roots require reviewed active configuration")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise CompositionError("active configuration metadata is unavailable") from exc
    if os.name == "posix" and metadata.st_uid != 0:
        raise CompositionError("active configuration must be owned by root")
    if stat.S_IMODE(metadata.st_mode) != 0o444:
        raise CompositionError("active configuration must have mode 0444")
    return value


def read_secret(path: Path) -> bytes:
    try:
        metadata = path.stat()
        secret = path.read_bytes()
    except OSError as exc:
        raise CompositionError("service protocol key is unavailable") from exc
    if path.is_symlink():
        raise CompositionError("service protocol key must not be a symbolic link")
    if os.name == "posix" and (
        stat.S_IMODE(metadata.st_mode) != 0o440
        or metadata.st_uid != 0
        or metadata.st_gid != 20000
    ):
        raise CompositionError("service protocol key ownership or mode is invalid")
    if secret.endswith(b"\n"):
        secret = secret[:-1]
    if len(secret) < 32:
        raise CompositionError("service protocol key must contain at least 32 bytes")
    return secret


def credential_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.stat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompositionError("service credential document is unavailable") from exc
    if path.is_symlink():
        raise CompositionError(
            "service credential document must not be a symbolic link"
        )
    if os.name == "posix" and (
        stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid()
    ):
        raise CompositionError("service credential ownership or mode is invalid")
    if not isinstance(value, dict):
        raise CompositionError("service credential document must be an object")
    return value


def private_key_path(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CompositionError("worker gateway private key is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or (
        os.name == "posix"
        and (stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid())
    ):
        raise CompositionError(
            "worker gateway private key ownership or mode is invalid"
        )
    return path


def reviewed_windows_overlay_port(value: object) -> int:
    if value != 9443:
        raise CompositionError("windows_overlay_bind_port must be 9443")
    return 9443


def require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CompositionError(f"{name} must be a non-empty canonical string")
    return value


def operation_timeouts(
    config: Mapping[str, Any],
    *,
    server_role: str,
    service_roles: Mapping[str, ServiceRole] = SERVICE_ROLES,
) -> Mapping[str, float]:
    configured = config.get("timeouts")
    if not isinstance(configured, Mapping):
        raise CompositionError("service timeout configuration is unavailable")
    try:
        read = float(configured["read_connector_seconds"]) + 5
        side_effect = float(configured["side_effect_connector_seconds"]) + 5
        terminal = (
            float(configured["terminal_seconds"])
            + 2 * WorkerExecutionLimits().cancellation_grace_seconds
            + 5
        )
        active = float(configured["active_request_seconds"]) + 5
    except (KeyError, TypeError, ValueError) as exc:
        raise CompositionError("service timeout configuration is invalid") from exc
    role = service_roles[server_role]
    if server_role == "capability_broker":
        return {"receive": active}
    if server_role == "orchestration_agent":
        return {"run": active, "cancel": side_effect}
    if server_role == "worker_gateway":
        return {operation: terminal for operation in role.operations}
    if server_role in {"google_connector", "knowledge_vault_connector"}:
        return {
            operation: (
                read
                if operation in {"read", "current"}
                or operation.startswith(("gmail_", "drive_"))
                else side_effect
            )
            for operation in role.operations
        }
    return {operation: side_effect for operation in role.operations}


def vault_write_timeout(config: Mapping[str, Any]) -> float:
    configured = config.get("timeouts")
    if not isinstance(configured, Mapping):
        raise CompositionError("service timeout configuration is unavailable")
    try:
        timeout = float(configured["side_effect_connector_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CompositionError("service timeout configuration is invalid") from exc
    if not 0 < timeout <= 120:
        raise CompositionError("vault write timeout is outside the connector bound")
    return timeout


def minimum_free_bytes(config: Mapping[str, Any]) -> int:
    bounds = config.get("resource_bounds")
    if not isinstance(bounds, Mapping):
        raise CompositionError("resource bound configuration is unavailable")
    gib = bounds.get("minimum_free_disk_gib")
    if isinstance(gib, bool) or not isinstance(gib, int) or gib < 0:
        raise CompositionError("minimum free disk configuration is invalid")
    return gib * 1024 * 1024 * 1024


def make_client(
    config: Mapping[str, Any],
    *,
    client_identity: str,
    server_role: str,
    service_roles: Mapping[str, ServiceRole],
    read_secret: Any,
    operation_timeouts: Any,
    client_factory: Any,
) -> Any:
    server = service_roles[server_role]
    client_role = next(
        (
            role.name
            for role in service_roles.values()
            if role.identity == client_identity
        ),
        None,
    )
    if client_role is None:
        raise CompositionError("service client identity has no deployment role")
    return client_factory(
        identity=client_identity,
        expected_server_identity=server.identity,
        secret=read_secret(Path("/run/protocol") / f"{client_role}--{server_role}.key"),
        host=server_role,
        port=server.port,
        operation_timeouts=operation_timeouts(config, server_role=server_role),
    )
