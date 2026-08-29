"""Configuration parsing and validation for the deployment bundle."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from ..acceptance_failpoints import reviewed_post_dispatch_failpoint_from_config
from ..knowledge_vault_writes import canonical_allowed_note_directories
from .deployment_models import EXPECTED_IDENTITIES, BundleValidationError

CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "release_id",
        "artifact_lock",
        "configuration_kind",
        "identities",
        "deployment",
        "models",
        "connector_allowlists",
        "egress",
        "paths",
        "permissions",
        "openwa_handoff",
        "timeouts",
        "retention",
        "resource_bounds",
    }
)
OPTIONAL_CONFIG_KEYS = frozenset({"acceptance_failpoint"})


def _load_mapping(path: Path, errors: list[str], label: str) -> dict[str, Any]:
    try:
        if path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".toml":
            value = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
        yaml.YAMLError,
    ) as exc:
        errors.append(f"{label} cannot be parsed: {type(exc).__name__}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    return value


def _validate_configuration(
    config: Mapping[str, Any],
    errors: list[str],
    *,
    expected_identities: Mapping[str, str] = EXPECTED_IDENTITIES,
    failpoint_parser: Callable[
        [object], object
    ] = reviewed_post_dispatch_failpoint_from_config,
    note_directory_parser: Callable[
        [object], object
    ] = canonical_allowed_note_directories,
) -> None:
    for key in sorted(set(config) - (CONFIG_KEYS | OPTIONAL_CONFIG_KEYS)):
        errors.append(f"unknown configuration key: {key}")
    for key in sorted(CONFIG_KEYS - set(config)):
        errors.append(f"missing configuration key: {key}")
    if config.get("schema_version") != 1:
        errors.append("configuration schema_version must be 1")
    if config.get("release_id") != "jarvis-assistant-v1":
        errors.append("release_id must be jarvis-assistant-v1")
    if config.get("artifact_lock") != "artifacts.lock.json":
        errors.append("artifact_lock must reference artifacts.lock.json")
    if config.get("configuration_kind") not in {"example", "active"}:
        errors.append("configuration_kind must be example or active")

    if "acceptance_failpoint" in config:
        try:
            configured_failpoint = failpoint_parser(config["acceptance_failpoint"])
        except (TypeError, ValueError) as exc:
            errors.append(f"acceptance_failpoint is invalid: {exc}")
        else:
            if (
                configured_failpoint is not None
                and config.get("configuration_kind") != "active"
            ):
                errors.append(
                    "acceptance_failpoint may only be enabled in active configuration"
                )

    identities = config.get("identities")
    if not isinstance(identities, Mapping):
        errors.append("identities must be an object")
    else:
        for service, expected in expected_identities.items():
            if identities.get(service) != expected:
                errors.append(f"service identity mismatch for {service}")
        for key in sorted(set(identities) - set(expected_identities)):
            errors.append(f"unknown service identity: {key}")
        values = tuple(identities.values())
        if len(values) != len(set(values)):
            errors.append("service identities must be distinct")

    permissions = config.get("permissions")
    expected_permissions = {
        "configuration": "0444",
        "credential_directory": "0700",
        "credential_file": "0600",
        "protocol_key": "0440",
        "ubuntu_worker_socket": "0600",
    }
    if permissions != expected_permissions:
        errors.append("deployment permissions do not match the reviewed contract")

    paths = config.get("paths")
    expected_paths = {
        "state": "/var/lib/jarvis/state",
        "audit": "/var/lib/jarvis/audit",
        "traces": "/var/lib/jarvis/traces",
        "deleted_conversations": "/var/lib/jarvis/deleted-conversations",
        "ubuntu_worker_socket": "/run/jarvis-worker/ubuntu.sock",
    }
    if paths != expected_paths:
        errors.append("deployment paths do not match the reviewed contract")

    deployment = config.get("deployment")
    deployment_keys = {
        "operator_id",
        "openwa_internal_session_id",
        "openwa_named_session",
        "openwa_operator_conversation_id",
        "google_subject",
        "oauth_callback_url",
        "windows_worker_identity",
        "ubuntu_worker_identity",
        "vault_remote",
        "vault_note_directories",
    }
    if not isinstance(deployment, Mapping) or set(deployment) != deployment_keys:
        errors.append("deployment identity and endpoint configuration is incomplete")
    else:
        for key in deployment_keys - {"vault_note_directories"}:
            value = deployment.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"deployment value {key} must be non-empty")
        callback = deployment.get("oauth_callback_url")
        try:
            parsed_callback = urlsplit(str(callback))
        except ValueError:
            parsed_callback = None
        if (
            parsed_callback is None
            or parsed_callback.scheme != "https"
            or parsed_callback.hostname is None
            or parsed_callback.username is not None
            or parsed_callback.password is not None
            or parsed_callback.path != "/callback"
            or parsed_callback.query
            or parsed_callback.fragment
        ):
            errors.append("oauth_callback_url must be a registered HTTPS /callback URL")
        note_directories = deployment.get("vault_note_directories")
        try:
            note_directory_parser(note_directories)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            errors.append("vault_note_directories must contain canonical unique paths")
        if config.get("configuration_kind") == "active" and any(
            "example" in str(value).lower() for value in deployment.values()
        ):
            errors.append("active configuration contains example deployment values")

    models = config.get("models")
    expected_models = {
        "default_model": "gpt-5.6-terra",
        "default_reasoning": "medium",
        "allowed_models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        "allowed_reasoning": ["none", "low", "medium", "high", "xhigh", "max"],
    }
    if models != expected_models:
        errors.append("model policy does not match the canonical V1 choices")

    allowlists = config.get("connector_allowlists")
    expected_allowlists = {
        "gmail": ["read", "search", "send", "reply"],
        "drive": ["read", "search"],
        "vault": ["read", "search", "write_markdown"],
    }
    if allowlists != expected_allowlists:
        errors.append("connector allowlists do not match the V1 capability surface")

    egress = config.get("egress")
    expected_egress = {
        "orchestration_hosts": ["api.openai.com"],
        "google_hosts": [
            "accounts.google.com",
            "oauth2.googleapis.com",
            "gmail.googleapis.com",
            "openidconnect.googleapis.com",
            "www.googleapis.com",
        ],
        "vault_hosts": ["vault.example.invalid"],
        "worker_overlay_network": "jarvis-worker-overlay",
    }
    if config.get("configuration_kind") == "example":
        if egress != expected_egress:
            errors.append(
                "egress policy does not match the reviewed connector boundaries"
            )
    elif not isinstance(egress, Mapping):
        errors.append("egress policy must be configured")
    else:
        vault_remote = (
            deployment.get("vault_remote") if isinstance(deployment, Mapping) else ""
        )
        vault_host = urlsplit(str(vault_remote)).hostname
        if (
            egress.get("orchestration_hosts") != ["api.openai.com"]
            or egress.get("google_hosts") != expected_egress["google_hosts"]
            or egress.get("vault_hosts") != [vault_host]
            or egress.get("worker_overlay_network")
            != expected_egress["worker_overlay_network"]
        ):
            errors.append(
                "active egress policy is inconsistent with connector endpoints"
            )

    timeouts = config.get("timeouts")
    expected_timeouts = {
        "model_turn_seconds": 90,
        "read_connector_seconds": 20,
        "side_effect_connector_seconds": 30,
        "terminal_seconds": 120,
        "active_request_seconds": 480,
    }
    if not isinstance(timeouts, Mapping) or set(timeouts) != set(expected_timeouts):
        errors.append("timeouts must define every conservative V1 bound")
    elif any(
        isinstance(timeouts[key], bool)
        or not isinstance(timeouts[key], int)
        or not 0 < timeouts[key] <= maximum
        for key, maximum in expected_timeouts.items()
    ):
        errors.append("timeouts must be positive and no greater than V1 maxima")

    retention = config.get("retention")
    expected_retention = {
        "conversation_history": "indefinite",
        "durable_memory": "until-explicit-forget",
        "audit": "indefinite",
        "diagnostic_traces": "indefinite",
        "backup_snapshots": "indefinite",
        "terminal_operational_days": 30,
    }
    if retention != expected_retention:
        errors.append("retention settings do not match the V1 contract")

    handoff = config.get("openwa_handoff")
    if not isinstance(handoff, Mapping) or handoff.get("activation") != "manual-only":
        errors.append("OpenWA handoff must be declared manual-only")
    elif handoff.get("members") != ["openwa", "inbound_receiver"]:
        errors.append("OpenWA handoff must describe exactly two future members")

    bounds = config.get("resource_bounds")
    if not isinstance(bounds, Mapping):
        errors.append("resource_bounds must be an object")
    else:
        expected = {
            "aggregate_memory_mib_max": 1280,
            "aggregate_cpu_cores_max": 2.0,
            "aggregate_pids": 512,
            "minimum_free_disk_gib": 2,
            "terminal_stdout_bytes": 1_048_576,
            "terminal_stderr_bytes": 1_048_576,
        }
        if bounds != expected:
            errors.append("resource_bounds do not match the fixed V1 limits")


def validate_configuration(config: Mapping[str, Any]) -> None:
    """Validate one configuration document independently of bundle artifacts."""

    errors: list[str] = []
    _validate_configuration(config, errors)
    if errors:
        raise BundleValidationError(tuple(dict.fromkeys(errors)))
