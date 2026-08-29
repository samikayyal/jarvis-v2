"""Administrative status and authenticated service-access policy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def service_access(
    role_name: str, role: Any
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Return the exact authenticated peers and operations for one service."""

    allowed_identities = {
        "capability_broker": ("jarvis-inbound",),
        "audit_service": ("jarvis-broker", "jarvis-google"),
        "google_connector": (
            "jarvis-broker",
            "jarvis-orchestration",
            "jarvis-oauth-callback",
        ),
        "knowledge_vault_connector": (
            "jarvis-broker",
            "jarvis-orchestration",
        ),
    }.get(role_name, ("jarvis-broker",))
    operation_allowlists: dict[str, tuple[str, ...]] = {
        identity: role.operations for identity in allowed_identities
    }
    if role_name == "capability_broker":
        operation_allowlists["jarvis-inbound"] = ("receive",)
    elif role_name == "audit_service":
        operation_allowlists = {
            "jarvis-broker": ("append", "append_batch", "writable"),
            "jarvis-google": ("append", "append_batch"),
        }
    elif role_name == "google_connector":
        operation_allowlists = {
            "jarvis-broker": tuple(
                operation
                for operation in role.operations
                if operation.startswith("action_")
                or operation in {"current", "start_authorization", "disconnect"}
            ),
            "jarvis-orchestration": tuple(
                operation
                for operation in role.operations
                if operation == "current_connection_generation"
                or operation.startswith(("gmail_", "drive_"))
            ),
            "jarvis-oauth-callback": ("oauth_callback",),
        }
    elif role_name == "knowledge_vault_connector":
        operation_allowlists = {
            "jarvis-broker": tuple(
                operation for operation in role.operations if operation != "read"
            ),
            "jarvis-orchestration": ("read",),
        }
    return allowed_identities, operation_allowlists


def administrative_status(
    config: Mapping[str, Any],
    *,
    artifact_lock_path: Path,
    runtime: Any,
) -> dict[str, object]:
    """Return the local administrator's bounded, content-free status view."""

    try:
        messaging = runtime._client(
            config,
            client_identity="jarvis-broker",
            server_role="openwa_outbound_connector",
        ).call("current")
        messaging_ready = bool(getattr(messaging, "messaging_ready", False))
    except (OSError, RuntimeError, TypeError, ValueError):
        messaging_ready = False
    try:
        workers = runtime._client(
            config,
            client_identity="jarvis-broker",
            server_role="worker_gateway",
        ).call("current")
        hosts = {
            "ubuntu": getattr(workers, "ubuntu", "unavailable"),
            "windows": getattr(workers, "windows", "unavailable"),
        }
    except (OSError, RuntimeError, TypeError, ValueError):
        hosts = {"ubuntu": "unavailable", "windows": "unavailable"}
    try:
        audit_writable = bool(
            runtime._client(
                config,
                client_identity="jarvis-broker",
                server_role="audit_service",
            ).call("writable")
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        audit_writable = False

    paths = config.get("paths")
    bounds = config.get("resource_bounds")
    try:
        free = runtime.shutil.disk_usage(str(paths["state"])).free
        minimum = int(bounds["minimum_free_disk_gib"]) * 1024**3
        resource_pressure = "ok" if free >= minimum else "degraded"
    except (KeyError, OSError, TypeError, ValueError):
        resource_pressure = "unknown"

    try:
        lock = json.loads(artifact_lock_path.read_text(encoding="utf-8"))
        application = lock["application"]
        release = {
            "id": config.get("release_id"),
            "version": application["version"],
            "revision": application["git_revision"],
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        release = {"id": config.get("release_id"), "version": None, "revision": None}

    return {
        "messaging_ready": messaging_ready,
        "audit_writable": audit_writable,
        "backup_freshness": "host-check-required",
        "hosts": hosts,
        "release": release,
        "resource_pressure": resource_pressure,
    }
