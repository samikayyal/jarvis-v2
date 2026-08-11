"""Ticket 27 unactivated deployment-bundle contract tests."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace

import pytest
import yaml

from jarvis_control_plane.codex_runtime import CodexCliAdapter
from jarvis_control_plane.codex_specialist import CodexExecutionEnvelope
from jarvis_control_plane.deployment import (
    BundleValidationError,
    validate_configuration,
    verify_bundle,
)
from jarvis_control_plane.deployment import (
    administrative_status as deployment_administrative_status,
)
from jarvis_control_plane.models import SignedInboundEvent
from jarvis_control_plane.openwa import OpenWAReadiness
from jarvis_control_plane.ports import TraceCapacityError, WorkerReadiness
from jarvis_control_plane.service_runtime import (
    SERVICE_ROLES,
    CompositionError,
    _AsyncIngressAdmission,
    _broker_state_store,
    _load_configuration,
    _operation_timeouts,
    _orchestration_operations,
    _service_access,
    _verified_inbound_event,
)
from jarvis_control_plane.service_runtime import (
    administrative_status as service_administrative_status,
)
from jarvis_control_plane.sessions import SQLiteWorkingSessionStore
from jarvis_control_plane.traces import SQLiteDiagnosticTraceStore

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
    assert report.aggregate_memory_mib == 1008
    assert report.aggregate_cpus == pytest.approx(1.80)
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
    npm_lock = yaml.safe_load(
        (SHIPPED_BUNDLE / "codex/package-lock.json").read_text("utf-8")
    )
    assert (
        npm_lock["packages"]["node_modules/@openai/codex"]["integrity"]
        == (lock["codex_cli"]["integrity"])
    )


def test_orchestration_composition_includes_the_codex_specialist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    captured: dict[str, object] = {}
    specialist = object()
    monkeypatch.setattr(runtime, "_credential_json", lambda _path: {"api_key": "key"})
    monkeypatch.setattr(runtime, "_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        runtime, "_service_trace", lambda *_args, **_kwargs: (None, None, object())
    )
    monkeypatch.setattr(runtime, "CodexCliAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(runtime, "GitCodexWorkspaceInspector", lambda: object())
    monkeypatch.setattr(runtime, "CodexSpecialist", lambda **_kwargs: specialist)

    def orchestration(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            run=lambda _request: None,
            cancel=lambda *, request_id: request_id == "request-01",
        )

    monkeypatch.setattr(runtime, "AgentsSdkOrchestrationAdapter", orchestration)
    operations = _orchestration_operations(
        {
            "models": {
                "default_model": "gpt-5.6-terra",
                "default_reasoning": "medium",
            },
            "timeouts": {"codex_seconds": 300, "model_turn_seconds": 90},
        }
    )

    assert operations.keys() == {"run", "cancel"}
    assert captured["codex_specialist"] is specialist
    assert captured["model_turn_timeout_seconds"] == 90
    identities, allowlists = _service_access(
        "orchestration_agent", SERVICE_ROLES["orchestration_agent"]
    )
    assert identities == ("jarvis-broker",)
    assert allowlists["jarvis-broker"] == ("run", "cancel")


def test_deployed_codex_cli_preserves_only_the_reviewed_proxy_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import jarvis_control_plane.codex_runtime as runtime

    executable = tmp_path / "codex"
    executable.write_text("pinned", encoding="utf-8")
    captured: dict[str, object] = {}
    final = json.dumps(
        {
            "status": "completed",
            "summary": "Reviewed.",
            "changed_paths": [],
            "test_evidence": [],
            "unresolved_questions": [],
        }
    )

    class Process:
        returncode = 0
        pid = 1

        def communicate(self, _prompt: str, *, timeout: float) -> tuple[str, str]:
            assert timeout > 0
            return (
                "\n".join(
                    (
                        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": final},
                            }
                        ),
                    )
                ),
                "",
            )

    def popen(_command: object, **kwargs: object) -> Process:
        captured.update(kwargs)
        return Process()

    monkeypatch.setenv("HTTPS_PROXY", "http://orchestration_egress_proxy:9080")
    monkeypatch.setenv("HTTP_PROXY", "http://orchestration_egress_proxy:9080")
    monkeypatch.setenv("NO_PROXY", "google_connector,knowledge_vault_connector")
    monkeypatch.setenv("UNREVIEWED_SECRET", "must-not-cross")
    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    adapter = CodexCliAdapter(executable=executable, api_key="api-key")

    adapter.invoke(
        CodexExecutionEnvelope(
            request_id="request-1",
            task="Review the workspace.",
            host="ubuntu",
            cwd="/srv/jarvis-workspace",
            model="gpt-5.6-terra",
            reasoning="medium",
            sandbox="read-only",
            approval_policy="on-request",
            timeout_seconds=300,
            operation="review",
            allowed_paths=(),
            proposal_digest=None,
        ),
        deadline=monotonic() + 1,
    )

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["HTTPS_PROXY"] == "http://orchestration_egress_proxy:9080"
    assert environment["HTTP_PROXY"] == "http://orchestration_egress_proxy:9080"
    assert environment["NO_PROXY"] == "google_connector,knowledge_vault_connector"
    assert "UNREVIEWED_SECRET" not in environment


def test_working_session_store_is_usable_from_service_handler_threads(
    tmp_path: Path,
) -> None:
    store = SQLiteWorkingSessionStore(tmp_path / "sessions.sqlite3")
    results: list[object] = []

    thread = Thread(target=lambda: results.append(store.load()))
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert results == [None]


def test_durable_ingress_admission_returns_before_background_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    dispatch_started = Event()
    release_dispatch = Event()

    class Receiver:
        def admit(self, _event: object) -> object:
            return SimpleNamespace(disposition="admitted", status_code=202)

    class Worker:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def run_once(self) -> object | None:
            self.calls += 1
            if self.calls == 1:
                dispatch_started.set()
                assert release_dispatch.wait(timeout=2)
                return SimpleNamespace(disposition="dispatched")
            return None

    monkeypatch.setattr(runtime, "OpenWAIngressWorker", Worker)
    admission = _AsyncIngressAdmission(
        receiver=Receiver(),  # type: ignore[arg-type]
        state=object(),
    )

    result = admission.receive(object())  # type: ignore[arg-type]

    assert result.status_code == 202
    assert dispatch_started.wait(timeout=1)
    release_dispatch.set()


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


def test_fresh_google_authorization_requests_only_identity_and_read_scopes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    captured: dict[str, object] = {}

    class Client:
        def call(self, operation: str, **kwargs: object) -> str:
            captured.update(operation=operation, **kwargs)
            return "https://accounts.google.com/o/oauth2/v2/auth?state=test"

    monkeypatch.setenv("JARVIS_SERVICE_IDENTITY", "jarvis-broker")
    monkeypatch.setattr(runtime, "_load_configuration", lambda _path: {})
    monkeypatch.setattr(runtime, "_client", lambda *_args, **_kwargs: Client())

    assert runtime.main(["google-authorize", "--operation-id", "connect-01"]) == 0

    requested_scopes = set(captured["requested_scopes"])
    assert captured["operation"] == "start_authorization"
    assert captured["operation_id"] == "connect-01"
    assert "openid" in requested_scopes
    assert "https://www.googleapis.com/auth/gmail.readonly" in requested_scopes
    assert (
        "https://www.googleapis.com/auth/calendar.events.readonly" in requested_scopes
    )
    assert "https://www.googleapis.com/auth/drive.readonly" in requested_scopes
    assert "https://www.googleapis.com/auth/gmail.send" not in requested_scopes
    assert "https://www.googleapis.com/auth/calendar.events" not in requested_scopes
    assert capsys.readouterr().out.startswith("https://accounts.google.com/")


@pytest.mark.parametrize(
    ("access", "expected_scope", "excluded_scope"),
    (
        (
            "gmail-send",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/calendar.events",
        ),
        (
            "calendar-write",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/gmail.send",
        ),
    ),
)
def test_google_administration_requests_only_the_named_incremental_write_scope(
    monkeypatch: pytest.MonkeyPatch,
    access: str,
    expected_scope: str,
    excluded_scope: str,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    captured: dict[str, object] = {}

    class Client:
        def call(self, operation: str, **kwargs: object) -> str:
            captured.update(operation=operation, **kwargs)
            return "https://accounts.google.com/o/oauth2/v2/auth?state=test"

    monkeypatch.setenv("JARVIS_SERVICE_IDENTITY", "jarvis-broker")
    monkeypatch.setattr(runtime, "_load_configuration", lambda _path: {})
    monkeypatch.setattr(runtime, "_client", lambda *_args, **_kwargs: Client())

    assert (
        runtime.main(
            [
                "google-authorize",
                "--operation-id",
                f"enable-{access}",
                "--access",
                access,
            ]
        )
        == 0
    )

    requested_scopes = set(captured["requested_scopes"])
    assert expected_scope in requested_scopes
    assert excluded_scope not in requested_scopes


def test_google_startup_does_not_revive_a_persisted_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    class StateStore:
        def __init__(self, _database: str) -> None:
            self.set_calls: list[object] = []

        def get_connection(self) -> object:
            return SimpleNamespace(connected=False)

        def set_connection(self, **kwargs: object) -> None:
            self.set_calls.append(kwargs)

    state = StateStore("unused")
    credential_store = SimpleNamespace(
        current=SimpleNamespace(granted_scopes=frozenset({"scope"})),
        delete_calls=0,
    )

    def delete_credential() -> None:
        credential_store.delete_calls += 1

    credential_store.delete = delete_credential
    monkeypatch.setattr(
        runtime,
        "_credential_json",
        lambda _path: {"client_id": "id", "client_secret": "secret"},
    )
    monkeypatch.setattr(
        runtime,
        "FileGoogleCredentialStore",
        lambda _path: credential_store,
    )
    monkeypatch.setattr(runtime, "SQLiteGoogleOAuthStateStore", lambda _path: state)
    monkeypatch.setattr(
        runtime,
        "_service_trace",
        lambda *_args, **_kwargs: (object(), object(), object()),
    )
    monkeypatch.setattr(runtime, "_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "RemoteAuditBoundary", lambda _client: object())
    monkeypatch.setattr(
        runtime,
        "GoogleLiveOAuthProvider",
        lambda **_kwargs: SimpleNamespace(authorization_url=lambda _value: "url"),
    )
    monkeypatch.setattr(
        runtime,
        "GoogleOAuthLifecycle",
        lambda **_kwargs: SimpleNamespace(
            connection_binding=object(),
            start_authorization=lambda **_kwargs: object(),
            handle_callback=lambda **_kwargs: object(),
            disconnect=lambda: object(),
            handle_refresh_failure=lambda *_args, **_kwargs: object(),
        ),
    )

    class Reads:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __getattr__(self, _name: str):
            return lambda **_kwargs: object()

    monkeypatch.setattr(runtime, "GoogleReadConnector", Reads)
    monkeypatch.setattr(runtime, "GoogleApiReadProvider", lambda **_kwargs: object())
    monkeypatch.setattr(runtime, "GmailWriteConnector", lambda **_kwargs: object())
    monkeypatch.setattr(runtime, "GmailApiWriteProvider", lambda **_kwargs: object())
    monkeypatch.setattr(runtime, "CalendarActionDispatcher", lambda **_kwargs: object())
    monkeypatch.setattr(
        runtime, "GoogleApiCalendarWriteProvider", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        runtime,
        "OwnedActionService",
        lambda _owner: SimpleNamespace(
            operations=lambda: {
                name: (lambda: None)
                for name in SERVICE_ROLES["google_connector"].operations
                if name.startswith("action_")
            }
        ),
    )

    operations = runtime._google_operations(
        {
            "deployment": {
                "google_subject": "subject-01",
                "oauth_callback_url": "https://oauth.jarvis.invalid/callback",
            },
            "resource_bounds": {"minimum_free_disk_gib": 2},
            "timeouts": {
                "read_connector_seconds": 20,
                "side_effect_connector_seconds": 30,
                "terminal_seconds": 120,
                "active_request_seconds": 480,
            },
        }
    )

    assert set(operations) == set(SERVICE_ROLES["google_connector"].operations)
    assert state.set_calls == []
    assert credential_store.delete_calls == 1


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


def test_administrative_status_reports_safe_operational_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tomllib.loads(
        (SHIPPED_BUNDLE / "config.example.toml").read_text(encoding="utf-8")
    )

    class Client:
        def __init__(self, role: str) -> None:
            self.role = role

        def call(self, operation: str) -> object:
            if self.role == "audit_service":
                assert operation == "writable"
                return True
            assert operation == "current"
            if self.role == "openwa_outbound_connector":
                return OpenWAReadiness(True, "ready")
            return WorkerReadiness(ubuntu="ready", windows="unavailable")

    monkeypatch.setattr(
        "jarvis_control_plane.service_runtime._client",
        lambda _config, *, client_identity, server_role: Client(server_role),
    )
    monkeypatch.setattr(
        "jarvis_control_plane.service_runtime.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=3 * 1024**3),
    )

    dependency_status = service_administrative_status(
        config,
        artifact_lock_path=SHIPPED_BUNDLE / "artifacts.lock.json",
    )

    services = verify_bundle(SHIPPED_BUNDLE, source_root=REPOSITORY_ROOT).services
    calls = 0

    def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        {"Service": service, "State": "running", "Health": "healthy"}
                        for service in services
                    ]
                )
            )
        return SimpleNamespace(stdout=json.dumps(dependency_status))

    status = deployment_administrative_status(SHIPPED_BUNDLE, runner=run)

    assert set(status) == {
        "components",
        "messaging_ready",
        "audit_writable",
        "backup_freshness",
        "hosts",
        "release",
        "resource_pressure",
    }
    assert set(status["components"].values()) == {"ready"}
    assert status["messaging_ready"] is True
    assert status["hosts"] == {"ubuntu": "ready", "windows": "unavailable"}
    assert status["audit_writable"] is True
    assert status["backup_freshness"] == "not-configured"
    assert status["resource_pressure"] == "ok"


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
    assert "RUN npm ci --omit=dev --ignore-scripts" in (
        SHIPPED_BUNDLE / "Dockerfile"
    ).read_text("utf-8")

    active = tomllib.loads((SHIPPED_BUNDLE / "config.example.toml").read_text("utf-8"))
    active["configuration_kind"] = "active"
    active["egress"]["worker_overlay_network"] = "wrong-overlay"
    with pytest.raises(BundleValidationError, match="active egress policy"):
        validate_configuration(active)


def test_service_transport_timeouts_cover_configured_operation_deadlines() -> None:
    config = tomllib.loads((SHIPPED_BUNDLE / "config.example.toml").read_text("utf-8"))
    assert _operation_timeouts(config, server_role="capability_broker")["receive"] > 480
    assert _operation_timeouts(config, server_role="orchestration_agent")["run"] > 480
    worker = _operation_timeouts(config, server_role="worker_gateway")
    assert set(worker.values()) == {125.0}


def test_trace_admission_preserves_the_configured_free_disk_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jarvis_control_plane.traces.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=150),
    )
    store = SQLiteDiagnosticTraceStore(
        tmp_path / "traces.sqlite3",
        capacity_bytes=1000,
        reservation_bytes=100,
        hard_max_bytes=100,
        minimum_free_bytes=100,
    )
    with pytest.raises(TraceCapacityError) as raised:
        store.reserve(request_id="request-low-disk", reservation_bytes=51)
    assert raised.value.available_bytes == 50


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
        "codex/package.json",
        "codex/package-lock.json",
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
