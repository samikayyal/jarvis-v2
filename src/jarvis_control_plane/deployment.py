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
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import yaml


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
    "openwa-handoff.md",
    "requirements.lock",
)

RESOURCE_LIMITS: Mapping[str, ServiceResourceLimits] = MappingProxyType(
    {
        "inbound_receiver": ServiceResourceLimits("64M", Decimal("0.10"), 32),
        "capability_broker": ServiceResourceLimits("192M", Decimal("0.35"), 64),
        "orchestration_agent": ServiceResourceLimits("256M", Decimal("0.45"), 128),
        "audit_service": ServiceResourceLimits("64M", Decimal("0.10"), 32),
        "google_connector": ServiceResourceLimits("96M", Decimal("0.15"), 64),
        "knowledge_vault_connector": ServiceResourceLimits("128M", Decimal("0.20"), 64),
        "openwa_outbound_connector": ServiceResourceLimits("64M", Decimal("0.10"), 32),
        "worker_gateway": ServiceResourceLimits("96M", Decimal("0.25"), 64),
        "public_oauth_callback": ServiceResourceLimits("48M", Decimal("0.10"), 32),
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
    }
)

ALLOWED_CREDENTIAL_MOUNTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "inbound_receiver": frozenset(),
        "capability_broker": frozenset(),
        "orchestration_agent": frozenset({"/run/credentials/openai"}),
        "audit_service": frozenset(),
        "google_connector": frozenset({"/run/credentials/google"}),
        "knowledge_vault_connector": frozenset({"/run/credentials/vault"}),
        "openwa_outbound_connector": frozenset({"/run/credentials/openwa"}),
        "worker_gateway": frozenset({"/run/credentials/windows-worker"}),
        "public_oauth_callback": frozenset(),
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
        "ubuntu_worker_socket": "0660",
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
        note_directories = deployment.get("vault_note_directories")
        if not isinstance(note_directories, list) or not note_directories:
            errors.append("vault_note_directories must contain at least one path")
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
            or not isinstance(egress.get("worker_overlay_network"), str)
            or not egress.get("worker_overlay_network")
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
    if timeouts != expected_timeouts:
        errors.append("timeouts do not match the conservative V1 defaults")

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
            errors.append("resource_bounds do not match the reviewed V1 limits")


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
        "python_base_image",
        "uv_build_image",
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
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    from_instructions = tuple(
        line.strip() for line in dockerfile.splitlines() if line.startswith("FROM ")
    )
    expected_from = (
        f"FROM {uv_reference} AS uv",
        f"FROM {reference}",
    )
    if len(from_instructions) != 2 or not re.fullmatch(
        r"FROM python:3\.13\.13-slim-bookworm@sha256:[0-9a-f]{64}",
        from_instructions[-1] if from_instructions else "",
    ):
        errors.append("Dockerfile base image must be pinned by sha256 digest")
    elif from_instructions != expected_from:
        errors.append("Dockerfile images differ from artifact lock")
    if "RUN uv pip install" not in dockerfile or "RUN python -m pip" in dockerfile:
        errors.append("Dockerfile dependency installation must use uv")

    requirement = lock.get("requirements_lock")
    expected_hash = (
        requirement.get("sha256") if isinstance(requirement, Mapping) else None
    )
    if (
        not isinstance(requirement, Mapping)
        or requirement.get("path") != "requirements.lock"
    ):
        errors.append("requirements lock path must be requirements.lock")
    actual_hash = hashlib.sha256((root / "requirements.lock").read_bytes()).hexdigest()
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
        errors.append("compose must contain exactly the nine reviewed services")

    networks = compose.get("networks")
    handoff_active = isinstance(networks, Mapping) and "openwa-handoff" in networks
    if handoff_active:
        errors.append("production OpenWA handoff network must not be activated")
    egress_networks = {"orchestration_egress", "google_egress", "vault_egress"}
    if isinstance(networks, Mapping):
        for name, network in networks.items():
            if not isinstance(network, Mapping):
                errors.append(f"network {name} must be an object")
            elif name in egress_networks and network != {"internal": False}:
                errors.append(f"network {name} must be a dedicated egress segment")
            elif name == "worker_overlay" and network != {
                "external": True,
                "name": "jarvis-worker-overlay",
            }:
                errors.append(
                    "worker_overlay must reference the manual private overlay"
                )
            elif (
                name not in egress_networks | {"worker_overlay"}
                and network.get("internal") is not True
            ):
                errors.append(f"network {name} must be private and non-published")

    identities = config.get("identities", {})
    seen_users: set[str] = set()
    for service, expected_limits in RESOURCE_LIMITS.items():
        raw = services.get(service)
        if not isinstance(raw, Mapping):
            errors.append(f"missing compose service: {service}")
            continue
        if raw.get("profiles") != ["manual-activation"]:
            errors.append(f"{service} must remain behind the manual-activation profile")
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
        environment = raw.get("environment", {})
        expected_identity = (
            identities.get(service) if isinstance(identities, Mapping) else None
        )
        if (
            not isinstance(environment, Mapping)
            or environment.get("JARVIS_SERVICE_IDENTITY") != expected_identity
        ):
            errors.append(f"compose identity mismatch for {service}")
        if "healthcheck" not in raw:
            errors.append(f"{service} must define a healthcheck")
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
        allowed_host_source = parts[0] == "/etc/jarvis/jarvis.toml" or parts[
            0
        ].startswith("/etc/jarvis/credentials/")
        if "/var/run/docker.sock" in volume or (
            volume.startswith("/") and not allowed_host_source
        ):
            errors.append(f"{service} has a prohibited broad host mount")
    if not has_config:
        errors.append(f"{service} must mount the validated configuration read-only")
    actual_credentials = {t for t in targets if t.startswith("/run/credentials/")}
    if actual_credentials != ALLOWED_CREDENTIAL_MOUNTS[service]:
        errors.append(f"{service} credential mounts differ from the reviewed boundary")


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
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="deployment bundle directory")
    parser.add_argument("--configuration", type=Path)
    parser.add_argument("--source-root", type=Path)
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
    print(
        f"verified {report.release_id}: {len(report.services)} services, "
        f"{report.aggregate_memory_mib} MiB, {report.aggregate_cpus:.2f} CPU, "
        f"{report.aggregate_pids} PIDs; activation unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
