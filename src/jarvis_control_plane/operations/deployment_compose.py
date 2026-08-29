"""Compose service, network, volume, and resource contract validation."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from .deployment_models import (
    ALLOWED_CREDENTIAL_MOUNTS,
    ALLOWED_PROTOCOL_MOUNTS,
    ALLOWED_STATE_MOUNTS,
    RESOURCE_LIMITS,
    ServiceResourceLimits,
)


def _validate_service_volumes(
    service: str,
    volumes: object,
    errors: list[str],
    *,
    credential_mounts: Mapping[str, frozenset[str]] = ALLOWED_CREDENTIAL_MOUNTS,
    protocol_mounts: Mapping[str, frozenset[str]] = ALLOWED_PROTOCOL_MOUNTS,
    state_mounts: Mapping[str, frozenset[str]] = ALLOWED_STATE_MOUNTS,
) -> None:
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
            and target not in credential_mounts[service]
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
                parts[0] == target
                and target in state_mounts[service]
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
    if actual_credentials != credential_mounts[service]:
        errors.append(f"{service} credential mounts differ from the reviewed boundary")
    actual_protocol = {
        target
        for target in targets
        if target == "/run/protocol" or target.startswith("/run/protocol/")
    }
    if actual_protocol != protocol_mounts[service]:
        errors.append(
            f"{service} protocol-key mounts differ from the reviewed boundary"
        )
    actual_state = {target for target in targets if target in state_mounts[service]}
    if actual_state != state_mounts[service]:
        errors.append(f"{service} state mounts differ from the reviewed boundary")
    reviewed = {"/etc/jarvis/jarvis.toml:/run/jarvis/config.toml:ro"}
    reviewed.update(
        f"/etc/jarvis/credentials/{target.rsplit('/', 1)[-1]}:{target}"
        + ("" if target == "/run/credentials/google" else ":ro")
        for target in credential_mounts[service]
    )
    reviewed.update(
        f"/etc/jarvis/protocol/{target.rsplit('/', 1)[-1]}:{target}:ro"
        for target in protocol_mounts[service]
    )
    reviewed.update(
        (
            f"/run/jarvis/deleted-archive-ipc:{target}"
            if target == "/run/jarvis-deleted"
            else f"{target}:{target}"
        )
        for target in state_mounts[service]
    )
    if service == "worker_gateway":
        reviewed.add("/run/jarvis-worker/ubuntu.sock:/run/jarvis-worker/ubuntu.sock:ro")
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
    actual_pids = limits.get("pids") if isinstance(limits, Mapping) else None
    try:
        actual_cpu_decimal = Decimal(str(actual_cpus))
    except InvalidOperation:
        actual_cpu_decimal = Decimal(-1)
    if actual_memory != expected.memory:
        errors.append(f"{service} memory limit must be {expected.memory}")
    if actual_cpu_decimal != expected.cpus:
        errors.append(f"{service} CPU limit must be {expected.cpus}")
    if actual_pids != expected.pids:
        errors.append(f"{service} deploy PID limit must be {expected.pids}")
    if raw.get("pids_limit") != expected.pids:
        errors.append(f"{service} PID limit must be {expected.pids}")


def _validate_compose(
    compose: Mapping[str, Any],
    config: Mapping[str, Any],
    errors: list[str],
    *,
    resource_limits: Mapping[str, ServiceResourceLimits] = RESOURCE_LIMITS,
    credential_mounts: Mapping[str, frozenset[str]] = ALLOWED_CREDENTIAL_MOUNTS,
    protocol_mounts: Mapping[str, frozenset[str]] = ALLOWED_PROTOCOL_MOUNTS,
    state_mounts: Mapping[str, frozenset[str]] = ALLOWED_STATE_MOUNTS,
    validate_volumes: Callable[..., None] = _validate_service_volumes,
    validate_resources: Callable[..., None] = _validate_service_resources,
) -> bool:
    services = compose.get("services")
    if not isinstance(services, Mapping):
        errors.append("compose services must be an object")
        return False
    if set(services) != set(resource_limits):
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
        resource_limits.items(), start=1
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
        probe = (
            [
                "CMD",
                "bash",
                "-c",
                (
                    "exec 3<>/dev/tcp/127.0.0.1/9080; "
                    'printf "GET /health HTTP/1.1\\r\\nHost: localhost\\r\\n\\r\\n" '
                    ">&3; IFS= read -r status <&3; "
                    '[[ "$$status" == "HTTP/1.1 200 OK"$\'\\r\' ]]'
                ),
            ]
            if service.endswith("_egress_proxy")
            else ["CMD", "python", "/opt/jarvis/deployment/health_probe.py"]
        )
        expected_healthcheck = {
            "test": probe,
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "10m" if service.endswith("_egress_proxy") else "5m",
        }
        if service.endswith("_egress_proxy"):
            expected_healthcheck["start_interval"] = "30s"
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
        validate_volumes(
            service,
            raw.get("volumes", []),
            errors,
            credential_mounts=credential_mounts,
            protocol_mounts=protocol_mounts,
            state_mounts=state_mounts,
        )
        validate_resources(service, raw, expected_limits, errors)
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
