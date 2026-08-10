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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

REQUIRED_FILES = (
    "Dockerfile",
    "README.md",
    "artifacts.lock.json",
    "compose.yaml",
    "config.example.json",
    "openwa-handoff.md",
    "requirements.lock",
)

RESOURCE_LIMITS: Mapping[str, tuple[str, Decimal, int]] = MappingProxyType(
    {
        "inbound_receiver": ("64M", Decimal("0.10"), 32),
        "capability_broker": ("192M", Decimal("0.35"), 64),
        "orchestration_agent": ("256M", Decimal("0.45"), 128),
        "audit_service": ("64M", Decimal("0.10"), 32),
        "google_connector": ("96M", Decimal("0.15"), 64),
        "knowledge_vault_connector": ("128M", Decimal("0.20"), 64),
        "openwa_outbound_connector": ("64M", Decimal("0.10"), 32),
        "worker_gateway": ("96M", Decimal("0.25"), 64),
        "public_oauth_callback": ("48M", Decimal("0.10"), 32),
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
        "identities",
        "paths",
        "permissions",
        "openwa_handoff",
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


def verify_bundle(bundle: str | Path) -> BundleVerificationReport:
    """Validate one bundle without invoking any external program or service."""

    root = Path(bundle).resolve()
    errors: list[str] = []
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            errors.append(f"missing bundle file: {relative}")
    if errors:
        raise BundleValidationError(errors)

    compose = _load_mapping(root / "compose.yaml", errors, "compose")
    config = _load_mapping(root / "config.example.json", errors, "configuration")
    lock = _load_mapping(root / "artifacts.lock.json", errors, "artifact lock")

    _validate_configuration(config, errors)
    _validate_artifacts(root, lock, errors)
    handoff_active = _validate_compose(compose, config, errors)
    _validate_handoff_description(root / "openwa-handoff.md", errors)

    if errors:
        raise BundleValidationError(tuple(dict.fromkeys(errors)))

    services = tuple(sorted(RESOURCE_LIMITS))
    return BundleVerificationReport(
        release_id=str(config["release_id"]),
        services=services,
        aggregate_memory_mib=sum(_memory_mib(v[0]) for v in RESOURCE_LIMITS.values()),
        aggregate_cpus=float(sum(v[1] for v in RESOURCE_LIMITS.values())),
        aggregate_pids=sum(v[2] for v in RESOURCE_LIMITS.values()),
        openwa_handoff_activated=handoff_active,
        checked_files=REQUIRED_FILES,
    )


def _load_mapping(path: Path, errors: list[str], label: str) -> dict[str, Any]:
    try:
        if path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
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


def _validate_artifacts(root: Path, lock: Mapping[str, Any], errors: list[str]) -> None:
    if set(lock) != {
        "schema_version",
        "application",
        "python_base_image",
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
    base = lock.get("python_base_image")
    reference = base.get("reference") if isinstance(base, Mapping) else None
    if not isinstance(reference, str) or not re.fullmatch(
        r"python:3\.13\.13-slim-bookworm@sha256:[0-9a-f]{64}", reference
    ):
        errors.append("Python base image must be pinned by tag and sha256 digest")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    first_instruction = next(
        (line.strip() for line in dockerfile.splitlines() if line.strip()), ""
    )
    if not re.fullmatch(
        r"FROM python:3\.13\.13-slim-bookworm@sha256:[0-9a-f]{64}",
        first_instruction,
    ):
        errors.append("Dockerfile base image must be pinned by sha256 digest")
    elif reference is not None and first_instruction != f"FROM {reference}":
        errors.append("Dockerfile base image differs from artifact lock")

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
    if isinstance(networks, Mapping):
        for name, network in networks.items():
            if not isinstance(network, Mapping) or network.get("internal") is not True:
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
        if target == "/run/jarvis/config.json" and parts[-1] == "ro":
            has_config = True
        if (
            target.startswith("/run/credentials/")
            and target not in ALLOWED_CREDENTIAL_MOUNTS[service]
        ):
            errors.append(f"{service} has an unauthorized credential mount")
        if "/var/run/docker.sock" in volume or volume.startswith("/"):
            errors.append(f"{service} has a prohibited broad host mount")
    if not has_config:
        errors.append(f"{service} must mount the validated configuration read-only")
    actual_credentials = {t for t in targets if t.startswith("/run/credentials/")}
    if actual_credentials != ALLOWED_CREDENTIAL_MOUNTS[service]:
        errors.append(f"{service} credential mounts differ from the reviewed boundary")


def _validate_service_resources(
    service: str,
    raw: Mapping[str, Any],
    expected: tuple[str, Decimal, int],
    errors: list[str],
) -> None:
    memory, cpus, pids = expected
    deploy = raw.get("deploy")
    resources = deploy.get("resources") if isinstance(deploy, Mapping) else None
    limits = resources.get("limits") if isinstance(resources, Mapping) else None
    actual_memory = limits.get("memory") if isinstance(limits, Mapping) else None
    actual_cpus = limits.get("cpus") if isinstance(limits, Mapping) else None
    try:
        actual_cpu_decimal = Decimal(str(actual_cpus))
    except InvalidOperation:
        actual_cpu_decimal = Decimal(-1)
    if actual_memory != memory:
        errors.append(f"{service} memory limit must be {memory}")
    if actual_cpu_decimal != cpus:
        errors.append(f"{service} CPU limit must be {cpus}")
    if raw.get("pids_limit") != pids:
        errors.append(f"{service} PID limit must be {pids}")


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="deployment bundle directory")
    args = parser.parse_args(argv)
    try:
        report = verify_bundle(args.bundle)
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
