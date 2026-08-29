"""Static Ticket 27 deployment-bundle contract tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from jarvis_control_plane.deployment import (
    BundleValidationError,
    validate_configuration,
    verify_bundle,
)

from .helpers import (
    REPOSITORY_ROOT,
    SHIPPED_BUNDLE,
    _copy_bundle,
)


def test_shipped_bundle_is_complete_pinned_and_unactivated() -> None:
    report = verify_bundle(SHIPPED_BUNDLE, source_root=REPOSITORY_ROOT)

    assert report.release_id == "jarvis-assistant-v1"
    assert 'ENTRYPOINT ["python", "-m", "jarvis_control_plane.service_runtime"]' in (
        SHIPPED_BUNDLE / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert report.services == (
        "audit_service",
        "capability_broker",
        "deleted_conversation_archive",
        "google_connector",
        "google_egress_proxy",
        "inbound_receiver",
        "knowledge_vault_connector",
        "openwa_outbound_connector",
        "orchestration_agent",
        "orchestration_egress_proxy",
        "public_oauth_callback",
        "vault_egress_proxy",
        "worker_gateway",
    )
    assert report.aggregate_memory_mib == 1056
    assert report.aggregate_cpus == pytest.approx(1.89)
    assert report.aggregate_pids == 512
    assert report.openwa_handoff_activated is False
    assert report.host_mutations == ()
    compose = yaml.safe_load((SHIPPED_BUNDLE / "compose.yaml").read_text("utf-8"))
    commands = {
        name: tuple(service["command"]) for name, service in compose["services"].items()
    }
    assert commands == {
        name: (
            ("serve-egress-proxy", name.removesuffix("_egress_proxy"))
            if name.endswith("_egress_proxy")
            else ("serve", name)
        )
        for name in report.services
    }
    assert compose["services"]["capability_broker"]["depends_on"] == {
        "deleted_conversation_archive": {"condition": "service_healthy"}
    }
    assert {
        service: {
            key: compose["services"][service]["healthcheck"][key]
            for key in ("start_period", "start_interval")
        }
        for service in report.services
        if service.endswith("_egress_proxy")
    } == {
        "google_egress_proxy": {"start_period": "10m", "start_interval": "30s"},
        "orchestration_egress_proxy": {
            "start_period": "10m",
            "start_interval": "30s",
        },
        "vault_egress_proxy": {"start_period": "10m", "start_interval": "30s"},
    }
    assert {
        service: compose["services"][service]["deploy"]["resources"]["limits"]["memory"]
        for service in report.services
        if service.endswith("_egress_proxy")
    } == {
        "google_egress_proxy": "48M",
        "orchestration_egress_proxy": "48M",
        "vault_egress_proxy": "48M",
    }


def test_bundle_separates_deleted_content() -> None:
    compose = yaml.safe_load((SHIPPED_BUNDLE / "compose.yaml").read_text("utf-8"))
    services = compose["services"]
    broker_mounts = tuple(services["capability_broker"]["volumes"])
    archive_mounts = tuple(services["deleted_conversation_archive"]["volumes"])

    assert not any(
        mount.endswith(":/var/lib/jarvis/deleted-conversations")
        for mount in broker_mounts
    )
    assert any(
        mount.endswith(":/var/lib/jarvis/deleted-conversations")
        for mount in archive_mounts
    )
    assert services["deleted_conversation_archive"]["user"] == "10010:20000"
    assert services["deleted_conversation_archive"]["network_mode"] == "none"
    config = (SHIPPED_BUNDLE / "config.example.toml").read_text("utf-8")
    assert "openidconnect.googleapis.com" in config


def test_bundle_rejects_unknown_configuration_and_identity_mismatch(
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(tmp_path)
    config_path = bundle / "config.example.toml"
    config = config_path.read_text(encoding="utf-8").replace(
        'openwa_outbound_connector = "jarvis-openwa-outbound"',
        'openwa_outbound_connector = "jarvis-inbound"',
    )
    config_path.write_text(f"unexpected = true\n{config}", encoding="utf-8")

    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)

    assert "unknown configuration key: unexpected" in raised.value.errors
    assert (
        "service identity mismatch for openwa_outbound_connector" in raised.value.errors
    )


@pytest.mark.parametrize(
    "note_directories",
    ([1], ["Notes", "Notes"], ["/Notes"], [".private"]),
)
def test_configuration_rejects_noncanonical_vault_note_directories(
    note_directories: list[object],
) -> None:
    config = tomllib.loads(
        (SHIPPED_BUNDLE / "config.example.toml").read_text(encoding="utf-8")
    )
    config["deployment"]["vault_note_directories"] = note_directories

    with pytest.raises(BundleValidationError) as raised:
        validate_configuration(config)

    assert (
        "vault_note_directories must contain canonical unique paths"
        in raised.value.errors
    )


def test_configuration_allows_lower_bounds_and_requires_https_callback() -> None:
    config = tomllib.loads(
        (SHIPPED_BUNDLE / "config.example.toml").read_text(encoding="utf-8")
    )
    config["timeouts"]["model_turn_seconds"] = 60

    validate_configuration(config)

    config["deployment"]["oauth_callback_url"] = "http://oauth.example.invalid/callback"
    with pytest.raises(BundleValidationError) as raised:
        validate_configuration(config)

    assert (
        "oauth_callback_url must be a registered HTTPS /callback URL"
        in raised.value.errors
    )


def test_bundle_rejects_floating_or_unlocked_artifacts(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    dockerfile = bundle / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").replace(
            "python:3.13.13-slim-bookworm@sha256:",
            "python:latest # sha256:",
        ),
        encoding="utf-8",
    )

    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)

    assert (
        "Dockerfile base image must be pinned by sha256 digest" in raised.value.errors
    )


def test_bundle_rejects_unpinned_or_unpreflighted_native_python(
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(tmp_path)
    readme = bundle / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        .replace(
            "/opt/jarvis/python/cpython-3.13.13-linux-x86_64-gnu/bin/python3.13",
            "3.13",
        )
        .replace("--property=MemoryDenyWriteExecute=yes", ""),
        encoding="utf-8",
    )

    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)

    assert (
        "native host runtime installation is not pinned and hardening-preflighted"
        in raised.value.errors
    )


def test_bundle_rejects_security_network_and_resource_regressions(
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(tmp_path)
    compose_path = bundle / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    receiver = compose["services"]["inbound_receiver"]
    receiver["privileged"] = True
    receiver["networks"].append("openwa-handoff")
    receiver["deploy"]["resources"]["limits"]["memory"] = "2G"
    receiver["deploy"]["resources"]["limits"]["pids"] = 31
    compose["networks"]["openwa-handoff"] = {"external": True}
    compose_path.write_text(yaml.safe_dump(compose), encoding="utf-8")

    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)

    assert "inbound_receiver must not use privileged mode" in raised.value.errors
    assert (
        "production OpenWA handoff network must not be activated" in raised.value.errors
    )
    assert "inbound_receiver memory limit must be 64M" in raised.value.errors
    assert "inbound_receiver deploy PID limit must be 32" in raised.value.errors


def test_bundle_routes_credentialed_egress_only_through_allowlisted_proxies(
    tmp_path: Path,
) -> None:
    compose = yaml.safe_load((SHIPPED_BUNDLE / "compose.yaml").read_text("utf-8"))
    services = compose["services"]
    for connector, proxy, segment in (
        ("orchestration_agent", "orchestration_egress_proxy", "orchestration_egress"),
        ("google_connector", "google_egress_proxy", "google_egress"),
        ("knowledge_vault_connector", "vault_egress_proxy", "vault_egress"),
    ):
        assert segment in services[connector]["networks"]
        assert "external_egress" not in services[connector]["networks"]
        assert set(services[proxy]["networks"]) == {segment, "external_egress"}
    assert compose["networks"]["orchestration_egress"] == {"internal": True}
    assert compose["networks"]["google_egress"] == {"internal": True}
    assert compose["networks"]["vault_egress"] == {"internal": True}

    bundle = _copy_bundle(tmp_path)
    compose_path = bundle / "compose.yaml"
    mutated = yaml.safe_load(compose_path.read_text("utf-8"))
    mutated["services"]["google_connector"]["networks"].append("external_egress")
    compose_path.write_text(yaml.safe_dump(mutated), encoding="utf-8")
    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)
    assert "google_connector must not bypass its egress proxy" in raised.value.errors


@pytest.mark.parametrize(
    ("service", "network"),
    [
        ("capability_broker", "external_egress"),
        ("deleted_conversation_archive", "external_egress"),
    ],
)
def test_bundle_rejects_network_access_outside_every_reviewed_service_set(
    tmp_path: Path, service: str, network: str
) -> None:
    bundle = _copy_bundle(tmp_path)
    compose_path = bundle / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text("utf-8"))
    target = compose["services"][service]
    target.pop("network_mode", None)
    target["networks"] = [network]
    compose_path.write_text(yaml.safe_dump(compose), encoding="utf-8")

    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)

    expected = (
        "deleted conversation archive"
        if service == "deleted_conversation_archive"
        else service
    )
    assert any(expected in error for error in raised.value.errors)


def test_openwa_route_worker_overlay_and_docker_context_are_reviewed() -> None:
    compose = yaml.safe_load((SHIPPED_BUNDLE / "compose.yaml").read_text("utf-8"))
    assert set(compose["services"]["openwa_outbound_connector"]["networks"]) == {
        "broker_openwa_outbound",
        "openwa_api",
    }
    assert compose["networks"]["openwa_api"] == {
        "external": True,
        "name": "jarvis-openwa-api",
    }
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text("utf-8").splitlines()
    assert "deployment/credentials" in dockerignore
    assert "deployment/credentials/**" in dockerignore
    dockerfile = (SHIPPED_BUNDLE / "Dockerfile").read_text("utf-8")
    assert (
        "useradd --uid 10006 --gid 20000 --home-dir /var/lib/jarvis/vault" in dockerfile
    )

    active = tomllib.loads((SHIPPED_BUNDLE / "config.example.toml").read_text("utf-8"))
    active["configuration_kind"] = "active"
    active["egress"]["worker_overlay_network"] = "wrong-overlay"
    with pytest.raises(BundleValidationError, match="active egress policy"):
        validate_configuration(active)


def test_bundle_rejects_credential_mount_leak_and_missing_health_logging(
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(tmp_path)
    compose_path = bundle / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    broker = compose["services"]["capability_broker"]
    broker["volumes"].append("./credentials/google:/run/credentials/google:ro")
    broker.pop("healthcheck")
    broker.pop("logging")
    compose_path.write_text(yaml.safe_dump(compose), encoding="utf-8")

    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)

    assert (
        "capability_broker has an unauthorized credential mount" in raised.value.errors
    )
    assert "capability_broker must define a healthcheck" in raised.value.errors
    assert (
        "capability_broker must define bounded rotated logging" in raised.value.errors
    )


def test_bundle_rejects_unreviewed_volume_tuple(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    compose_path = bundle / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["orchestration_agent"]["volumes"].append(
        "./payload:/opt/jarvis/src"
    )
    compose_path.write_text(yaml.safe_dump(compose), encoding="utf-8")

    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)

    assert (
        "orchestration_agent volumes differ from the reviewed boundary"
        in raised.value.errors
    )


def test_bundle_rejects_modified_healthcheck(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    compose_path = bundle / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["capability_broker"]["healthcheck"]["test"] = [
        "CMD",
        "true",
    ]
    compose_path.write_text(yaml.safe_dump(compose), encoding="utf-8")

    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)

    assert "capability_broker healthcheck differs from the reviewed probe" in (
        raised.value.errors
    )


def test_verification_is_static_and_declares_no_host_mutation_steps() -> None:
    report = verify_bundle(SHIPPED_BUNDLE, source_root=REPOSITORY_ROOT)

    assert report.checked_files == (
        "Dockerfile",
        "README.md",
        "artifacts.lock.json",
        "compose.yaml",
        "config.example.toml",
        "health_probe.py",
        "openwa-handoff.md",
        "requirements.lock",
        "systemd/jarvis-backup.service",
        "systemd/jarvis-backup.timer",
        "systemd/jarvis-ubuntu-worker.service",
        "windows/install-jarvis-worker.ps1",
    )
    assert report.host_mutations == ()


def test_bundle_rejects_verifier_only_or_cross_wired_service_commands(
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(tmp_path)
    compose_path = bundle / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["audit_service"]["command"] = [
        "serve",
        "orchestration_agent",
    ]
    compose_path.write_text(yaml.safe_dump(compose), encoding="utf-8")

    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)

    assert (
        "audit_service must run its role-specific composition root"
        in raised.value.errors
    )
