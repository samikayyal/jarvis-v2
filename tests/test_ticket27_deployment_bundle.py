"""Ticket 27 unactivated deployment-bundle contract tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from jarvis_control_plane.deployment import BundleValidationError, verify_bundle

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_BUNDLE = REPOSITORY_ROOT / "deployment"


def _copy_bundle(tmp_path: Path) -> Path:
    target = tmp_path / "deployment"
    shutil.copytree(SHIPPED_BUNDLE, target)
    return target


def test_shipped_bundle_is_complete_pinned_and_unactivated() -> None:
    report = verify_bundle(SHIPPED_BUNDLE, source_root=REPOSITORY_ROOT)

    assert report.release_id == "jarvis-assistant-v1"
    assert report.services == (
        "audit_service",
        "capability_broker",
        "google_connector",
        "inbound_receiver",
        "knowledge_vault_connector",
        "openwa_outbound_connector",
        "orchestration_agent",
        "public_oauth_callback",
        "worker_gateway",
    )
    assert report.aggregate_memory_mib == 1008
    assert report.aggregate_cpus == pytest.approx(1.8)
    assert report.aggregate_pids == 512
    assert report.openwa_handoff_activated is False
    assert report.host_mutations == ()


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
    compose["networks"]["openwa-handoff"] = {"external": True}
    compose_path.write_text(yaml.safe_dump(compose), encoding="utf-8")

    with pytest.raises(BundleValidationError) as raised:
        verify_bundle(bundle, source_root=REPOSITORY_ROOT)

    assert "inbound_receiver must not use privileged mode" in raised.value.errors
    assert (
        "production OpenWA handoff network must not be activated" in raised.value.errors
    )
    assert "inbound_receiver memory limit must be 64M" in raised.value.errors


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


def test_verification_is_static_and_declares_no_host_mutation_steps() -> None:
    report = verify_bundle(SHIPPED_BUNDLE, source_root=REPOSITORY_ROOT)

    assert report.checked_files == (
        "Dockerfile",
        "README.md",
        "artifacts.lock.json",
        "compose.yaml",
        "config.example.toml",
        "openwa-handoff.md",
        "requirements.lock",
    )
    assert report.host_mutations == ()
