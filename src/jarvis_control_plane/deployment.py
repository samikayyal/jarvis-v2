"""Offline verification for the unactivated Jarvis deployment bundle.

The verifier is intentionally static: it reads only the supplied bundle and
never invokes Docker, opens a network connection, provisions credentials, or
changes host state.  Production activation remains a separate manual action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import yaml

from .knowledge_vault_writes import canonical_allowed_note_directories


@dataclass(frozen=True, slots=True)
class ServiceResourceLimits:
    memory: str
    cpus: Decimal
    pids: int


REQUIRED_FILES = (
    "Dockerfile",
    "README.md",
    "artifacts.lock.json",
    "compose.yaml",
    "config.example.toml",
    "codex/package.json",
    "codex/package-lock.json",
    "openwa-handoff.md",
    "requirements.lock",
    "systemd/jarvis-backup.service",
    "systemd/jarvis-backup.timer",
)

RESOURCE_LIMITS: Mapping[str, ServiceResourceLimits] = MappingProxyType(
    {
        "inbound_receiver": ServiceResourceLimits("64M", Decimal("0.10"), 32),
        "capability_broker": ServiceResourceLimits("144M", Decimal("0.25"), 32),
        "orchestration_agent": ServiceResourceLimits("224M", Decimal("0.42"), 112),
        "audit_service": ServiceResourceLimits("64M", Decimal("0.10"), 32),
        "google_connector": ServiceResourceLimits("64M", Decimal("0.12"), 48),
        "knowledge_vault_connector": ServiceResourceLimits("96M", Decimal("0.17"), 48),
        "openwa_outbound_connector": ServiceResourceLimits("64M", Decimal("0.10"), 32),
        "worker_gateway": ServiceResourceLimits("96M", Decimal("0.25"), 64),
        "public_oauth_callback": ServiceResourceLimits("48M", Decimal("0.10"), 32),
        "deleted_conversation_archive": ServiceResourceLimits(
            "48M", Decimal("0.10"), 32
        ),
        "orchestration_egress_proxy": ServiceResourceLimits("32M", Decimal("0.03"), 16),
        "google_egress_proxy": ServiceResourceLimits("32M", Decimal("0.03"), 16),
        "vault_egress_proxy": ServiceResourceLimits("32M", Decimal("0.03"), 16),
    }
)

EXPECTED_IDENTITIES: Mapping[str, str] = MappingProxyType(
    {
        "inbound_receiver": "jarvis-inbound",
        "capability_broker": "jarvis-broker",
        "orchestration_agent": "jarvis-orchestration",
        "audit_service": "jarvis-audit",
        "google_connector": "jarvis-google",
        "knowledge_vault_connector": "jarvis-vault",
        "openwa_outbound_connector": "jarvis-openwa-outbound",
        "worker_gateway": "jarvis-worker-gateway",
        "public_oauth_callback": "jarvis-oauth-callback",
        "deleted_conversation_archive": "jarvis-deleted-archive",
        "orchestration_egress_proxy": "jarvis-orchestration-egress",
        "google_egress_proxy": "jarvis-google-egress",
        "vault_egress_proxy": "jarvis-vault-egress",
    }
)

ALLOWED_CREDENTIAL_MOUNTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "inbound_receiver": frozenset({"/run/credentials/openwa-inbound"}),
        "capability_broker": frozenset({"/run/credentials/broker"}),
        "orchestration_agent": frozenset({"/run/credentials/openai"}),
        "audit_service": frozenset(),
        "google_connector": frozenset({"/run/credentials/google"}),
        "knowledge_vault_connector": frozenset({"/run/credentials/vault"}),
        "openwa_outbound_connector": frozenset({"/run/credentials/openwa"}),
        "worker_gateway": frozenset({"/run/credentials/windows-worker"}),
        "public_oauth_callback": frozenset(),
        "deleted_conversation_archive": frozenset(),
        "orchestration_egress_proxy": frozenset(),
        "google_egress_proxy": frozenset(),
        "vault_egress_proxy": frozenset(),
    }
)

ALLOWED_PROTOCOL_MOUNTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "inbound_receiver": frozenset(
            {"/run/protocol/inbound_receiver--capability_broker.key"}
        ),
        "capability_broker": frozenset(
            {
                "/run/protocol/inbound_receiver--capability_broker.key",
                "/run/protocol/capability_broker--orchestration_agent.key",
                "/run/protocol/capability_broker--audit_service.key",
                "/run/protocol/capability_broker--google_connector.key",
                "/run/protocol/capability_broker--knowledge_vault_connector.key",
                "/run/protocol/capability_broker--openwa_outbound_connector.key",
                "/run/protocol/capability_broker--worker_gateway.key",
                "/run/protocol/capability_broker--deleted_conversation_archive.key",
            }
        ),
        "orchestration_agent": frozenset(
            {
                "/run/protocol/capability_broker--orchestration_agent.key",
                "/run/protocol/orchestration_agent--google_connector.key",
                "/run/protocol/orchestration_agent--knowledge_vault_connector.key",
            }
        ),
        "audit_service": frozenset(
            {
                "/run/protocol/capability_broker--audit_service.key",
                "/run/protocol/google_connector--audit_service.key",
            }
        ),
        "google_connector": frozenset(
            {
                "/run/protocol/capability_broker--google_connector.key",
                "/run/protocol/orchestration_agent--google_connector.key",
                "/run/protocol/public_oauth_callback--google_connector.key",
                "/run/protocol/google_connector--audit_service.key",
            }
        ),
        "knowledge_vault_connector": frozenset(
            {
                "/run/protocol/capability_broker--knowledge_vault_connector.key",
                "/run/protocol/orchestration_agent--knowledge_vault_connector.key",
            }
        ),
        "openwa_outbound_connector": frozenset(
            {"/run/protocol/capability_broker--openwa_outbound_connector.key"}
        ),
        "worker_gateway": frozenset(
            {"/run/protocol/capability_broker--worker_gateway.key"}
        ),
        "public_oauth_callback": frozenset(
            {"/run/protocol/public_oauth_callback--google_connector.key"}
        ),
        "deleted_conversation_archive": frozenset(
            {"/run/protocol/capability_broker--deleted_conversation_archive.key"}
        ),
        "orchestration_egress_proxy": frozenset(),
        "google_egress_proxy": frozenset(),
        "vault_egress_proxy": frozenset(),
    }
)

ALLOWED_STATE_MOUNTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "inbound_receiver": frozenset(),
        "capability_broker": frozenset(
            {
                "/var/lib/jarvis/state",
                "/var/lib/jarvis/traces",
                "/run/jarvis-deleted",
            }
        ),
        "orchestration_agent": frozenset({"/var/lib/jarvis/codex-traces"}),
        "audit_service": frozenset({"/var/lib/jarvis/audit"}),
        "google_connector": frozenset({"/var/lib/jarvis/google-traces"}),
        "knowledge_vault_connector": frozenset({"/var/lib/jarvis/vault"}),
        "openwa_outbound_connector": frozenset(),
        "worker_gateway": frozenset(),
        "public_oauth_callback": frozenset(),
        "deleted_conversation_archive": frozenset(
            {
                "/var/lib/jarvis/deleted-conversations",
                "/run/jarvis-deleted",
            }
        ),
        "orchestration_egress_proxy": frozenset(),
        "google_egress_proxy": frozenset(),
        "vault_egress_proxy": frozenset(),
    }
)

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


class BundleValidationError(ValueError):
    """One or more static deployment invariants failed."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class BundleVerificationReport:
    release_id: str
    services: tuple[str, ...]
    aggregate_memory_mib: int
    aggregate_cpus: float
    aggregate_pids: int
    openwa_handoff_activated: bool
    checked_files: tuple[str, ...]
    host_mutations: tuple[str, ...] = ()


def verify_bundle(
    bundle: str | Path,
    *,
    configuration: str | Path | None = None,
    source_root: str | Path | None = None,
) -> BundleVerificationReport:
    """Validate one bundle without invoking any external program or service."""

    root = Path(bundle).resolve()
    errors: list[str] = []
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            errors.append(f"missing bundle file: {relative}")
    if errors:
        raise BundleValidationError(errors)

    compose = _load_mapping(root / "compose.yaml", errors, "compose")
    config_path = (
        Path(configuration).resolve()
        if configuration is not None
        else root / "config.example.toml"
    )
    config = _load_mapping(config_path, errors, "configuration")
    lock = _load_mapping(root / "artifacts.lock.json", errors, "artifact lock")

    _validate_configuration(config, errors)
    _validate_artifacts(
        root,
        lock,
        errors,
        source_root=(
            Path(source_root).resolve()
            if source_root is not None
            else root.parent.resolve()
        ),
    )
    handoff_active = _validate_compose(compose, config, errors)
    _validate_handoff_description(root / "openwa-handoff.md", errors)
    _validate_backup_units(root / "systemd", errors)

    if errors:
        raise BundleValidationError(tuple(dict.fromkeys(errors)))

    services = tuple(sorted(RESOURCE_LIMITS))
    return BundleVerificationReport(
        release_id=str(config["release_id"]),
        services=services,
        aggregate_memory_mib=sum(
            _memory_mib(limit.memory) for limit in RESOURCE_LIMITS.values()
        ),
        aggregate_cpus=float(sum(limit.cpus for limit in RESOURCE_LIMITS.values())),
        aggregate_pids=sum(limit.pids for limit in RESOURCE_LIMITS.values()),
        openwa_handoff_activated=handoff_active,
        checked_files=REQUIRED_FILES,
    )


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


def _validate_configuration(config: Mapping[str, Any], errors: list[str]) -> None:
    for key in sorted(set(config) - CONFIG_KEYS):
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

    identities = config.get("identities")
    if not isinstance(identities, Mapping):
        errors.append("identities must be an object")
    else:
        for service, expected in EXPECTED_IDENTITIES.items():
            if identities.get(service) != expected:
                errors.append(f"service identity mismatch for {service}")
        for key in sorted(set(identities) - set(EXPECTED_IDENTITIES)):
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
            canonical_allowed_note_directories(note_directories)  # type: ignore[arg-type]
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
        "calendar": ["read", "search", "create", "update"],
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
        "codex_seconds": 300,
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


def _validate_artifacts(
    root: Path,
    lock: Mapping[str, Any],
    errors: list[str],
    *,
    source_root: Path,
) -> None:
    if set(lock) != {
        "schema_version",
        "application",
        "database_schemas",
        "python_base_image",
        "uv_build_image",
        "node_build_image",
        "codex_cli",
        "os_packages",
        "requirements_lock",
    }:
        errors.append("artifact lock has missing or unknown keys")
        return
    if lock.get("schema_version") != 1:
        errors.append("artifact lock schema_version must be 1")
    application = lock.get("application")
    if (
        not isinstance(application, Mapping)
        or application.get("name") != "jarvis-v2"
        or application.get("version") != "0.1.0"
        or not re.fullmatch(r"[0-9a-f]{40}", str(application.get("git_revision", "")))
    ):
        errors.append("application artifact must be pinned to a Git revision")
    elif application.get("source_sha256") != _application_source_sha256(
        source_root, errors
    ):
        errors.append("application source differs from the pinned artifact")
    schemas = lock.get("database_schemas")
    expected_schemas = {
        "state",
        "sessions",
        "audit",
        "traces",
        "codex_traces",
        "google_traces",
        "deleted_conversations",
    }
    if (
        not isinstance(schemas, Mapping)
        or set(schemas) != expected_schemas
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in schemas.values()
        )
    ):
        errors.append("database schema fingerprints must be complete and pinned")
    base = lock.get("python_base_image")
    reference = base.get("reference") if isinstance(base, Mapping) else None
    if not isinstance(reference, str) or not re.fullmatch(
        r"python:3\.13\.13-slim-bookworm@sha256:[0-9a-f]{64}", reference
    ):
        errors.append("Python base image must be pinned by tag and sha256 digest")
    uv_image = lock.get("uv_build_image")
    uv_reference = uv_image.get("reference") if isinstance(uv_image, Mapping) else None
    if not isinstance(uv_reference, str) or not re.fullmatch(
        r"ghcr\.io/astral-sh/uv:0\.6\.14@sha256:[0-9a-f]{64}", uv_reference
    ):
        errors.append("uv build image must be pinned by tag and sha256 digest")
    node_image = lock.get("node_build_image")
    node_reference = (
        node_image.get("reference") if isinstance(node_image, Mapping) else None
    )
    if node_reference != (
        "node:24-bookworm-slim@sha256:"
        "65932751ed4073ed02f5c04e494e4b2572a891b7dbea0568a863dc80341bf848"
    ):
        errors.append("Node build image must be pinned by tag and sha256 digest")
    if lock.get("codex_cli") != {
        "package": "@openai/codex",
        "version": "0.147.0",
        "integrity": (
            "sha512-EQLEXecAG2ptxI7UpBMo2TR/ga5596/c/OsYF/0LoUDh5JANZ7IoGqlz"
            "BEWbuEVQ76JePIbtTW/ihCkp1a7Z3w=="
        ),
        "package_lock_sha256": (
            "dde6c5ad754926cb15527a834225cd9983887c3c4b1894a42d6c3888d4621c22"
        ),
    }:
        errors.append("Codex CLI artifact differs from the reviewed pin")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    from_instructions = tuple(
        line.strip() for line in dockerfile.splitlines() if line.startswith("FROM ")
    )
    expected_from = (
        f"FROM {node_reference} AS codex",
        f"FROM {uv_reference} AS uv",
        f"FROM {reference}",
    )
    if len(from_instructions) != 3 or not re.fullmatch(
        r"FROM python:3\.13\.13-slim-bookworm@sha256:[0-9a-f]{64}",
        from_instructions[-1] if from_instructions else "",
    ):
        errors.append("Dockerfile base image must be pinned by sha256 digest")
    elif from_instructions != expected_from:
        errors.append("Dockerfile images differ from artifact lock")
    codex_lock = _load_mapping(
        root / "codex/package-lock.json", errors, "Codex package lock"
    )
    codex_packages = (
        codex_lock.get("packages") if isinstance(codex_lock, Mapping) else None
    )
    codex_package = (
        codex_packages.get("node_modules/@openai/codex")
        if isinstance(codex_packages, Mapping)
        else None
    )
    if not isinstance(codex_package, Mapping) or {
        "version": codex_package.get("version"),
        "integrity": codex_package.get("integrity"),
    } != {
        "version": "0.147.0",
        "integrity": (
            "sha512-EQLEXecAG2ptxI7UpBMo2TR/ga5596/c/OsYF/0LoUDh5JANZ7IoGqlz"
            "BEWbuEVQ76JePIbtTW/ihCkp1a7Z3w=="
        ),
    }:
        errors.append("Codex npm lock does not match the reviewed artifact")
    codex_artifact = lock.get("codex_cli")
    if (
        codex_artifact.get("package_lock_sha256")
        if isinstance(codex_artifact, Mapping)
        else None
    ) != hashlib.sha256(
        (root / "codex/package-lock.json").read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest():
        errors.append("Codex npm lock digest differs from artifact lock")
    if "RUN npm ci --omit=dev --ignore-scripts" not in dockerfile:
        errors.append("Dockerfile must install Codex from the npm lock")
    if "RUN uv pip install" not in dockerfile or "RUN python -m pip" in dockerfile:
        errors.append("Dockerfile dependency installation must use uv")
    if (
        'ENTRYPOINT ["uv", "run", "--no-project", "python", "-m", '
        '"jarvis_control_plane.service_runtime"]'
    ) not in dockerfile:
        errors.append("Dockerfile must enter the role-specific service runtime")
    if lock.get("os_packages") != {
        "git": "1:2.39.5-0+deb12u3",
        "openssh-client": "1:9.2p1-2+deb12u10",
    }:
        errors.append("vault operating-system packages differ from the artifact lock")
    if "ARG GIT_VERSION=1:2.39.5-0+deb12u3" not in dockerfile:
        errors.append("Dockerfile must pin the vault Git client package")
    if "ARG OPENSSH_CLIENT_VERSION=1:9.2p1-2+deb12u10" not in dockerfile:
        errors.append("Dockerfile must pin the vault SSH client package")
    if (
        '"git=${GIT_VERSION}"' not in dockerfile
        or '"openssh-client=${OPENSSH_CLIENT_VERSION}"' not in dockerfile
    ):
        errors.append("Dockerfile must install only the pinned vault clients")

    requirement = lock.get("requirements_lock")
    expected_hash = (
        requirement.get("sha256") if isinstance(requirement, Mapping) else None
    )
    if (
        not isinstance(requirement, Mapping)
        or requirement.get("path") != "requirements.lock"
    ):
        errors.append("requirements lock path must be requirements.lock")
    actual_hash = hashlib.sha256(
        (root / "requirements.lock").read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()
    if expected_hash != actual_hash:
        errors.append("requirements.lock digest differs from artifact lock")


def _validate_compose(
    compose: Mapping[str, Any], config: Mapping[str, Any], errors: list[str]
) -> bool:
    services = compose.get("services")
    if not isinstance(services, Mapping):
        errors.append("compose services must be an object")
        return False
    if set(services) != set(RESOURCE_LIMITS):
        errors.append("compose must contain exactly the reviewed services")

    networks = compose.get("networks")
    handoff_active = isinstance(networks, Mapping) and "openwa-handoff" in networks
    if handoff_active:
        errors.append("production OpenWA handoff network must not be activated")
    connector_egress_networks = {
        "orchestration_egress",
        "google_egress",
        "vault_egress",
    }
    if isinstance(networks, Mapping):
        for name, network in networks.items():
            if not isinstance(network, Mapping):
                errors.append(f"network {name} must be an object")
            elif name in connector_egress_networks and network != {"internal": True}:
                errors.append(f"network {name} must terminate at its egress proxy")
            elif name == "external_egress" and network != {"internal": False}:
                errors.append(
                    "external_egress must be the sole Internet-routed segment"
                )
            elif name == "openwa_api" and network != {
                "external": True,
                "name": "jarvis-openwa-api",
            }:
                errors.append("openwa_api must reference the reviewed external route")
            elif name == "worker_overlay" and network != {
                "external": True,
                "name": "jarvis-worker-overlay",
            }:
                errors.append(
                    "worker_overlay must reference the manual private overlay"
                )
            elif (
                name
                not in connector_egress_networks
                | {"external_egress", "openwa_api", "worker_overlay"}
                and network.get("internal") is not True
            ):
                errors.append(f"network {name} must be private and non-published")

    identities = config.get("identities", {})
    seen_users: set[str] = set()
    for service_index, (service, expected_limits) in enumerate(
        RESOURCE_LIMITS.items(), start=1
    ):
        raw = services.get(service)
        if not isinstance(raw, Mapping):
            errors.append(f"missing compose service: {service}")
            continue
        if raw.get("profiles") != ["manual-activation"]:
            errors.append(f"{service} must remain behind the manual-activation profile")
        proxy_kind = service.removesuffix("_egress_proxy")
        expected_command = (
            ["serve-egress-proxy", proxy_kind]
            if service.endswith("_egress_proxy")
            else ["serve", service]
        )
        if raw.get("command") != expected_command:
            errors.append(f"{service} must run its role-specific composition root")
        if "image" in raw:
            errors.append(f"{service} must build only from the reviewed local artifact")
        build = raw.get("build")
        if build != {"context": "..", "dockerfile": "deployment/Dockerfile"}:
            errors.append(f"{service} build context differs from the reviewed artifact")
        if raw.get("privileged") not in {None, False}:
            errors.append(f"{service} must not use privileged mode")
        if raw.get("read_only") is not True:
            errors.append(f"{service} must use a read-only root filesystem")
        if raw.get("cap_drop") != ["ALL"]:
            errors.append(f"{service} must drop all Linux capabilities")
        if "no-new-privileges:true" not in raw.get("security_opt", []):
            errors.append(f"{service} must enforce no-new-privileges")
        if raw.get("pid") is not None or raw.get("ipc") == "host":
            errors.append(f"{service} must not join host namespaces")
        user = str(raw.get("user", ""))
        if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", user):
            errors.append(f"{service} must use a numeric non-root identity")
        elif user in seen_users:
            errors.append("container identities must be distinct")
        seen_users.add(user)
        if user != f"{10000 + service_index}:20000":
            errors.append(f"{service} identity differs from the reviewed UID/group")
        environment = raw.get("environment", {})
        expected_identity = (
            identities.get(service) if isinstance(identities, Mapping) else None
        )
        if (
            not isinstance(environment, Mapping)
            or environment.get("JARVIS_SERVICE_IDENTITY") != expected_identity
        ):
            errors.append(f"compose identity mismatch for {service}")
        healthcheck = raw.get("healthcheck")
        expected_healthcheck = {
            "test": [
                "CMD",
                "uv",
                "run",
                "--no-project",
                "python",
                "-m",
                "jarvis_control_plane.service_runtime",
                "proxy-health" if service.endswith("_egress_proxy") else "health",
            ],
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "10s",
        }
        if healthcheck is None:
            errors.append(f"{service} must define a healthcheck")
        elif healthcheck != expected_healthcheck:
            errors.append(f"{service} healthcheck differs from the reviewed probe")
        logging = raw.get("logging")
        options = logging.get("options", {}) if isinstance(logging, Mapping) else {}
        if not (
            isinstance(logging, Mapping)
            and logging.get("driver") == "local"
            and options.get("max-size") == "10m"
            and options.get("max-file") == "5"
        ):
            errors.append(f"{service} must define bounded rotated logging")
        _validate_service_volumes(service, raw.get("volumes", []), errors)
        _validate_service_resources(service, raw, expected_limits, errors)
        if raw.get("ports") and service != "public_oauth_callback":
            errors.append(f"{service} must not publish ports")

    callback = services.get("public_oauth_callback", {})
    if isinstance(callback, Mapping) and callback.get("ports") != [
        "127.0.0.1:8080:8080"
    ]:
        errors.append("OAuth callback must be the sole loopback-published endpoint")
    worker = services.get("worker_gateway", {})
    if isinstance(worker, Mapping) and worker.get("expose") != ["9443"]:
        errors.append("worker gateway must expose only the overlay mTLS session port")
    expected_egress_memberships = {
        "orchestration_agent": "orchestration_egress",
        "orchestration_egress_proxy": "orchestration_egress",
        "google_connector": "google_egress",
        "google_egress_proxy": "google_egress",
        "knowledge_vault_connector": "vault_egress",
        "vault_egress_proxy": "vault_egress",
    }
    expected_networks = {
        "inbound_receiver": {"ingress_broker"},
        "capability_broker": {
            "ingress_broker",
            "broker_orchestration",
            "broker_audit",
            "broker_google",
            "broker_vault",
            "broker_openwa_outbound",
            "broker_worker",
        },
        "orchestration_agent": {
            "broker_orchestration",
            "broker_google",
            "broker_vault",
            "orchestration_egress",
        },
        "audit_service": {"broker_audit"},
        "google_connector": {
            "broker_google",
            "broker_audit",
            "oauth_google",
            "google_egress",
        },
        "knowledge_vault_connector": {"broker_vault", "vault_egress"},
        "openwa_outbound_connector": {"broker_openwa_outbound", "openwa_api"},
        "worker_gateway": {"broker_worker", "worker_overlay"},
        "public_oauth_callback": {"oauth_google"},
        "orchestration_egress_proxy": {
            "orchestration_egress",
            "external_egress",
        },
        "google_egress_proxy": {"google_egress", "external_egress"},
        "vault_egress_proxy": {"vault_egress", "external_egress"},
    }
    for service, expected in expected_networks.items():
        raw = services.get(service, {})
        actual = raw.get("networks", []) if isinstance(raw, Mapping) else []
        if not isinstance(actual, list) or set(actual) != expected:
            errors.append(f"{service} networks differ from the reviewed topology")
        if isinstance(raw, Mapping) and raw.get("network_mode") is not None:
            errors.append(f"{service} must not override its reviewed network mode")
    archive = services.get("deleted_conversation_archive", {})
    if isinstance(archive, Mapping) and (
        archive.get("network_mode") != "none" or archive.get("networks") is not None
    ):
        errors.append("deleted conversation archive must remain networkless")
    for service, segment in expected_egress_memberships.items():
        raw = services.get(service, {})
        memberships = raw.get("networks", []) if isinstance(raw, Mapping) else []
        if segment not in memberships:
            errors.append(f"{service} must join its private egress segment")
        if service.endswith("_egress_proxy"):
            if set(memberships) != {segment, "external_egress"}:
                errors.append(f"{service} must be the sole egress bridge for {segment}")
        elif "external_egress" in memberships:
            errors.append(f"{service} must not bypass its egress proxy")
    outbound = services.get("openwa_outbound_connector", {})
    if isinstance(outbound, Mapping) and set(outbound.get("networks", [])) != {
        "broker_openwa_outbound",
        "openwa_api",
    }:
        errors.append("OpenWA outbound connector lacks its reviewed API route")
    return handoff_active


def _validate_service_volumes(service: str, volumes: object, errors: list[str]) -> None:
    if not isinstance(volumes, list):
        errors.append(f"{service} volumes must be a list")
        return
    targets: set[str] = set()
    has_config = False
    for volume in volumes:
        if not isinstance(volume, str):
            errors.append(f"{service} volume entries must use reviewed short syntax")
            continue
        parts = volume.split(":")
        if len(parts) < 2:
            errors.append(f"{service} has an invalid volume entry")
            continue
        target = parts[1]
        targets.add(target)
        if (
            parts[0] == "/etc/jarvis/jarvis.toml"
            and target == "/run/jarvis/config.toml"
            and parts[-1] == "ro"
        ):
            has_config = True
        if (
            target.startswith("/run/credentials/")
            and target not in ALLOWED_CREDENTIAL_MOUNTS[service]
        ):
            errors.append(f"{service} has an unauthorized credential mount")
        allowed_host_source = (
            parts[0] == "/etc/jarvis/jarvis.toml"
            or parts[0].startswith("/etc/jarvis/credentials/")
            or parts[0].startswith("/etc/jarvis/protocol")
            or (
                service == "worker_gateway"
                and parts[0] == "/run/jarvis-worker/ubuntu.sock"
                and target == "/run/jarvis-worker/ubuntu.sock"
                and parts[-1] == "ro"
            )
            or (
                parts[0] == "/run/jarvis/deleted-archive-ipc"
                and target == "/run/jarvis-deleted"
                and service in {"capability_broker", "deleted_conversation_archive"}
            )
            or (
                service == "orchestration_agent"
                and parts[0] == "/srv/jarvis-workspace"
                and target == "/srv/jarvis-workspace"
                and parts[-1] == "ro"
            )
            or (
                parts[0] == target
                and target in ALLOWED_STATE_MOUNTS[service]
                and len(parts) == 2
            )
        )
        if "/var/run/docker.sock" in volume or (
            volume.startswith("/") and not allowed_host_source
        ):
            errors.append(f"{service} has a prohibited broad host mount")
    if not has_config:
        errors.append(f"{service} must mount the validated configuration read-only")
    actual_credentials = {t for t in targets if t.startswith("/run/credentials/")}
    if actual_credentials != ALLOWED_CREDENTIAL_MOUNTS[service]:
        errors.append(f"{service} credential mounts differ from the reviewed boundary")
    actual_protocol = {
        target
        for target in targets
        if target == "/run/protocol" or target.startswith("/run/protocol/")
    }
    if actual_protocol != ALLOWED_PROTOCOL_MOUNTS[service]:
        errors.append(
            f"{service} protocol-key mounts differ from the reviewed boundary"
        )
    actual_state = {
        target for target in targets if target in ALLOWED_STATE_MOUNTS[service]
    }
    if actual_state != ALLOWED_STATE_MOUNTS[service]:
        errors.append(f"{service} state mounts differ from the reviewed boundary")
    reviewed = {"/etc/jarvis/jarvis.toml:/run/jarvis/config.toml:ro"}
    reviewed.update(
        f"/etc/jarvis/credentials/{target.rsplit('/', 1)[-1]}:{target}"
        + ("" if target == "/run/credentials/google" else ":ro")
        for target in ALLOWED_CREDENTIAL_MOUNTS[service]
    )
    reviewed.update(
        f"/etc/jarvis/protocol/{target.rsplit('/', 1)[-1]}:{target}:ro"
        for target in ALLOWED_PROTOCOL_MOUNTS[service]
    )
    reviewed.update(
        (
            f"/run/jarvis/deleted-archive-ipc:{target}"
            if target == "/run/jarvis-deleted"
            else f"{target}:{target}"
        )
        for target in ALLOWED_STATE_MOUNTS[service]
    )
    if service == "worker_gateway":
        reviewed.add("/run/jarvis-worker/ubuntu.sock:/run/jarvis-worker/ubuntu.sock:ro")
    if service == "orchestration_agent":
        reviewed.add("/srv/jarvis-workspace:/srv/jarvis-workspace:ro")
    actual = {volume for volume in volumes if isinstance(volume, str)}
    if actual != reviewed or len(actual) != len(volumes):
        errors.append(f"{service} volumes differ from the reviewed boundary")


def _validate_service_resources(
    service: str,
    raw: Mapping[str, Any],
    expected: ServiceResourceLimits,
    errors: list[str],
) -> None:
    deploy = raw.get("deploy")
    resources = deploy.get("resources") if isinstance(deploy, Mapping) else None
    limits = resources.get("limits") if isinstance(resources, Mapping) else None
    actual_memory = limits.get("memory") if isinstance(limits, Mapping) else None
    actual_cpus = limits.get("cpus") if isinstance(limits, Mapping) else None
    try:
        actual_cpu_decimal = Decimal(str(actual_cpus))
    except InvalidOperation:
        actual_cpu_decimal = Decimal(-1)
    if actual_memory != expected.memory:
        errors.append(f"{service} memory limit must be {expected.memory}")
    if actual_cpu_decimal != expected.cpus:
        errors.append(f"{service} CPU limit must be {expected.cpus}")
    if raw.get("pids_limit") != expected.pids:
        errors.append(f"{service} PID limit must be {expected.pids}")


def _validate_handoff_description(path: Path, errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    required = (
        "future two-member private handoff network",
        "OpenWA remains independently deployed",
        "must not be created or attached by this bundle",
        "complete OpenWA verification ladder",
    )
    for phrase in required:
        if phrase not in text:
            errors.append(f"OpenWA handoff description is missing: {phrase}")


def _validate_backup_units(root: Path, errors: list[str]) -> None:
    service = _unit_directives(
        (root / "jarvis-backup.service").read_text(encoding="utf-8")
    )
    timer = _unit_directives((root / "jarvis-backup.timer").read_text(encoding="utf-8"))
    expected_service = {
        ("Service", "Type"): ("oneshot",),
        ("Service", "User"): ("root",),
        ("Service", "UMask"): ("0077",),
        ("Service", "WorkingDirectory"): ("/opt/jarvis/current",),
        ("Service", "Environment"): ("PYTHONPATH=/opt/jarvis/current/src",),
        ("Service", "ExecStart"): (
            (
                "/opt/jarvis/current/.venv/bin/python -m "
                "jarvis_control_plane.administrative_backup create --kind nightly "
                "--artifact-lock /opt/jarvis/current/deployment/artifacts.lock.json"
            ),
        ),
    }
    expected_timer = {
        ("Timer", "OnCalendar"): ("*-*-* 02:00:00 UTC",),
        ("Timer", "Persistent"): ("true",),
        ("Timer", "RandomizedDelaySec"): ("15m",),
        ("Timer", "Unit"): ("jarvis-backup.service",),
    }
    if any(service.get(key) != value for key, value in expected_service.items()):
        errors.append("nightly backup service differs from the reviewed directives")
    if any(timer.get(key) != value for key, value in expected_timer.items()):
        errors.append("nightly backup timer differs from the reviewed directives")


def _unit_directives(text: str) -> dict[tuple[str, str], tuple[str, ...]]:
    section = ""
    values: dict[tuple[str, str], list[str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        directive = values.setdefault((section, key), [])
        if not value:
            directive.clear()
        else:
            directive.append(value)
    return {key: tuple(value) for key, value in values.items()}


def _memory_mib(value: str) -> int:
    return int(value.removesuffix("M"))


def _application_source_sha256(source_root: Path, errors: list[str]) -> str:
    paths = [source_root / "pyproject.toml", source_root / "README.md"]
    source_directory = source_root / "src"
    if source_directory.is_dir():
        paths.extend(sorted(source_directory.rglob("*.py")))
    if any(not path.is_file() for path in paths) or len(paths) == 2:
        errors.append("application source tree is incomplete")
        return ""
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(source_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def administrative_status(
    bundle: str | Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    backup_root: str | Path = "/var/backups/jarvis",
    now: datetime | None = None,
) -> dict[str, object]:
    """Combine local Compose health with authenticated dependency status."""

    compose = Path(bundle).resolve() / "compose.yaml"
    base = [
        "docker",
        "compose",
        "--file",
        str(compose),
        "--profile",
        "manual-activation",
    ]
    try:
        observed = runner(
            [*base, "ps", "--all", "--format", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
        rows = json.loads(observed.stdout or "[]")
        if isinstance(rows, Mapping):
            rows = [rows]
        if not isinstance(rows, list):
            raise TypeError("Compose status is not a list")
        by_service = {
            row.get("Service"): row for row in rows if isinstance(row, Mapping)
        }
        components = {
            service: (
                "ready"
                if by_service.get(service, {}).get("State") == "running"
                and by_service.get(service, {}).get("Health") in {"", "healthy"}
                else "unavailable"
            )
            for service in RESOURCE_LIMITS
        }
        dependency = runner(
            [*base, "run", "--rm", "capability_broker", "admin-status"],
            check=True,
            capture_output=True,
            text=True,
        )
        details = next(
            json.loads(line)
            for line in reversed(dependency.stdout.splitlines())
            if line.startswith("{")
        )
        if not isinstance(details, dict):
            raise TypeError("dependency status is not an object")
    except (
        OSError,
        StopIteration,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        raise RuntimeError("administrative status is unavailable") from exc
    details["backup_freshness"] = _backup_freshness(Path(backup_root), now=now)
    return {"components": components, **details}


def _backup_freshness(root: Path, *, now: datetime | None = None) -> str:
    try:
        manifests = [
            manifest
            for manifest in root.glob("*/manifest.json")
            if not manifest.parent.name.startswith(".partial-")
        ]
        if not manifests:
            return "missing"
        created = max(
            datetime.fromisoformat(
                json.loads(manifest.read_text(encoding="utf-8"))["created_at"]
            )
            for manifest in manifests
        )
        if created.tzinfo is None:
            raise ValueError
        age = (now or datetime.now(UTC)).astimezone(UTC) - created.astimezone(UTC)
        return "current" if timedelta(0) <= age <= timedelta(hours=36) else "stale"
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "invalid"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="deployment bundle directory")
    parser.add_argument("--configuration", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--administrative-status", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = verify_bundle(
            args.bundle,
            configuration=args.configuration,
            source_root=args.source_root,
        )
    except BundleValidationError as exc:
        for error in exc.errors:
            print(f"ERROR: {error}")
        return 1
    if args.administrative_status:
        print(json.dumps(administrative_status(args.bundle), sort_keys=True))
    else:
        print(
            f"verified {report.release_id}: {len(report.services)} services, "
            f"{report.aggregate_memory_mib} MiB, {report.aggregate_cpus:.2f} CPU, "
            f"{report.aggregate_pids} PIDs; activation unchanged"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
