"""Ticket 27 unactivated deployment-bundle contract tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from jarvis_control_plane.deployment import BundleValidationError, verify_bundle
from jarvis_control_plane.models import SignedInboundEvent
from jarvis_control_plane.service_runtime import (
    SERVICE_ROLES,
    CompositionError,
    _broker_state_store,
    _load_configuration,
    _orchestration_operations,
    _service_access,
    _verified_inbound_event,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_BUNDLE = REPOSITORY_ROOT / "deployment"


def _copy_bundle(tmp_path: Path) -> Path:
    target = tmp_path / "deployment"
    shutil.copytree(SHIPPED_BUNDLE, target)
    return target


def _active_configuration(tmp_path: Path) -> Path:
    content = (SHIPPED_BUNDLE / "config.example.toml").read_text(encoding="utf-8")
    replacements = {
        'configuration_kind = "example"': 'configuration_kind = "active"',
        "example-operator-id": "operator-01",
        "example-internal-session-id": "openwa-session-01",
        "example-named-session": "openwa-named-01",
        "example-operator-conversation-id": "conversation-01",
        "example-google-subject": "operator@jarvis.invalid",
        "https://oauth.example.invalid/callback": "https://oauth.jarvis.invalid/callback",
        "example-windows-worker": "windows-01",
        "example-ubuntu-worker": "ubuntu-01",
        "ssh://vault.example.invalid/notes.git": "ssh://vault.jarvis.invalid/notes.git",
        'vault_hosts = ["vault.example.invalid"]': 'vault_hosts = ["vault.jarvis.invalid"]',
    }
    for old, new in replacements.items():
        content = content.replace(old, new)
    path = tmp_path / "jarvis.toml"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o444)
    return path


def test_shipped_bundle_is_complete_pinned_and_unactivated() -> None:
    report = verify_bundle(SHIPPED_BUNDLE, source_root=REPOSITORY_ROOT)

    assert report.release_id == "jarvis-assistant-v1"
    assert report.services == (
        "audit_service",
        "capability_broker",
        "deleted_conversation_archive",
        "google_connector",
        "inbound_receiver",
        "knowledge_vault_connector",
        "openwa_outbound_connector",
        "orchestration_agent",
        "public_oauth_callback",
        "worker_gateway",
    )
    assert report.aggregate_memory_mib == 1056
    assert report.aggregate_cpus == pytest.approx(1.9)
    assert report.aggregate_pids == 512
    assert report.openwa_handoff_activated is False
    assert report.host_mutations == ()
    compose = yaml.safe_load((SHIPPED_BUNDLE / "compose.yaml").read_text("utf-8"))
    commands = {
        name: tuple(service["command"]) for name, service in compose["services"].items()
    }
    assert commands == {name: ("serve", name) for name in report.services}


def test_bundle_separates_deleted_content_and_composes_pinned_codex() -> None:
    compose = yaml.safe_load((SHIPPED_BUNDLE / "compose.yaml").read_text("utf-8"))
    services = compose["services"]
    broker_mounts = tuple(services["capability_broker"]["volumes"])
    archive_mounts = tuple(services["deleted_conversation_archive"]["volumes"])
    orchestration_mounts = tuple(services["orchestration_agent"]["volumes"])

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
    assert "/srv/jarvis-workspace:/srv/jarvis-workspace:ro" in orchestration_mounts

    lock = yaml.safe_load((SHIPPED_BUNDLE / "artifacts.lock.json").read_text("utf-8"))
    assert lock["codex_cli"] == {
        "package": "@openai/codex",
        "version": "0.147.0",
        "integrity": (
            "sha512-EQLEXecAG2ptxI7UpBMo2TR/ga5596/c/OsYF/0LoUDh5JANZ7IoGqlz"
            "BEWbuEVQ76JePIbtTW/ihCkp1a7Z3w=="
        ),
    }
    config = (SHIPPED_BUNDLE / "config.example.toml").read_text("utf-8")
    assert "openidconnect.googleapis.com" in config


def test_orchestration_composition_includes_the_codex_specialist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    captured: dict[str, object] = {}
    specialist = object()
    monkeypatch.setattr(runtime, "_credential_json", lambda _path: {"api_key": "key"})
    monkeypatch.setattr(runtime, "_client", lambda **_kwargs: object())
    monkeypatch.setattr(
        runtime, "_service_trace", lambda *_args, **_kwargs: (None, None, object())
    )
    monkeypatch.setattr(runtime, "CodexCliAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(runtime, "GitCodexWorkspaceInspector", lambda: object())
    monkeypatch.setattr(runtime, "CodexSpecialist", lambda **_kwargs: specialist)

    def orchestration(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(run=lambda _request: None)

    monkeypatch.setattr(runtime, "AgentsSdkOrchestrationAdapter", orchestration)
    operations = _orchestration_operations(
        {
            "models": {
                "default_model": "gpt-5.6-terra",
                "default_reasoning": "medium",
            },
            "timeouts": {"codex_seconds": 300},
        }
    )

    assert operations.keys() == {"run"}
    assert captured["codex_specialist"] is specialist


def test_broker_state_uses_only_the_write_only_deleted_archive_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    archive = object()
    captured: dict[str, object] = {}
    monkeypatch.setattr(runtime, "_read_secret", lambda _path: b"a" * 32)
    monkeypatch.setattr(
        runtime,
        "SQLiteDeletedConversationArchiveWriter",
        lambda endpoint, **kwargs: (
            captured.update(endpoint=endpoint, **kwargs) or archive
        ),
    )
    monkeypatch.setattr(
        runtime,
        "SQLiteDurableStateStore",
        lambda database, **kwargs: (
            captured.update(database=database, **kwargs) or object()
        ),
    )

    _broker_state_store(tmp_path)

    assert captured["endpoint"] == "/run/jarvis-deleted/writer.sock"
    assert captured["deleted_archive"] is archive
    assert captured["database"] == tmp_path / "state.sqlite3"


def test_google_administration_is_authenticated_and_not_model_accessible() -> None:
    identities, allowlists = _service_access(
        "google_connector", SERVICE_ROLES["google_connector"]
    )

    assert identities == (
        "jarvis-broker",
        "jarvis-orchestration",
        "jarvis-oauth-callback",
    )
    assert {"start_authorization", "disconnect"} <= set(allowlists["jarvis-broker"])
    assert "start_authorization" not in allowlists["jarvis-orchestration"]
    assert allowlists["jarvis-oauth-callback"] == ("oauth_callback",)


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


def test_runtime_rejects_unknown_active_configuration_before_binding(
    tmp_path: Path,
) -> None:
    path = _active_configuration(tmp_path)
    path.chmod(0o644)
    path.write_text(f"unexpected = true\n{path.read_text('utf-8')}", encoding="utf-8")
    path.chmod(0o444)

    with pytest.raises(CompositionError, match="failed validation"):
        _load_configuration(path)


def test_runtime_rejects_wrong_active_configuration_mode(tmp_path: Path) -> None:
    path = _active_configuration(tmp_path)
    path.chmod(0o644)

    with pytest.raises(CompositionError, match="mode 0444"):
        _load_configuration(path)


def test_inbound_receiver_verifies_raw_body_before_forwarding() -> None:
    secret = b"receiver-scoped-openwa-signing-secret"
    signed = SignedInboundEvent.from_mapping({"message": "exact"}, secret)

    assert _verified_inbound_event(signed.raw_body, signed.signature, secret) == signed
    assert (
        _verified_inbound_event(signed.raw_body + b" ", signed.signature, secret)
        is None
    )
    assert _verified_inbound_event(signed.raw_body, None, secret) is None
