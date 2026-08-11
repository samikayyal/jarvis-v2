"""Role-specific composition roots for the unactivated deployment bundle."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import urlopen

from .action_dispatch import RoutedActionDispatcher
from .adapters import (
    FixedModelAvailabilityProvider,
    SQLiteAuditBoundary,
    SQLiteDurableStateStore,
    SystemClock,
    UuidIdGenerator,
)
from .codex_runtime import CodexCliAdapter, GitCodexWorkspaceInspector
from .codex_specialist import (
    CodexSpecialist,
    CodexSpecialistConfig,
    CodexWorkspace,
)
from .control_plane import (
    ControlPlaneConfig,
    DeterministicCapabilityBroker,
    SignedMessageReceiver,
)
from .conversation_archive import (
    SQLiteDeletedConversationArchiveWriter,
    serve_sqlite_deleted_conversation_archive,
)
from .deployment import BundleValidationError, validate_configuration
from .egress_proxy import connect_through_proxy, serve_egress_proxy
from .gmail_actions import GMAIL_SEND_SCOPE
from .gmail_writes import GmailApiWriteProvider, GmailWriteConnector
from .google_calendar import (
    CALENDAR_WRITE_SCOPE,
    CalendarActionDispatcher,
    GoogleApiCalendarWriteProvider,
)
from .google_oauth import (
    GOOGLE_OAUTH_BASELINE_SCOPES,
    FileGoogleCredentialStore,
    GoogleLiveOAuthProvider,
    GoogleOAuthError,
    GoogleOAuthLifecycle,
    SQLiteGoogleOAuthStateStore,
)
from .google_reads import GoogleApiReadProvider, GoogleReadConnector
from .knowledge_vault import (
    KnowledgeVaultConnector,
    KnowledgeVaultReadResult,
    VaultReadInput,
)
from .knowledge_vault_writes import KnowledgeVaultWriteConnector
from .models import FrozenActionProposal, SignedInboundEvent
from .openwa import OpenWAConfig, OpenWAIngressWorker, OpenWAOutboundConnector
from .orchestration import AgentsSdkOrchestrationAdapter, BoundedReadTool
from .ports import ActionCancellationResult, ActionCancellationStatus
from .service_protocol import (
    AuthenticatedServiceClient,
    AuthenticatedServiceServer,
    OwnedActionService,
    RemoteActionDispatcher,
    RemoteAuditBoundary,
    RemoteMessagingReadinessProvider,
    RemoteOrchestrationAdapter,
    RemoteOutboundConnector,
    RemoteVaultProposalPreparer,
    RemoteWorkerReadinessProvider,
    _encode,
)
from .sessions import ModelAvailability, SQLiteWorkingSessionStore
from .traces import DiagnosticTraceRecorder, SQLiteDiagnosticTraceStore
from .ubuntu_worker import (
    UbuntuLocalPeerExpectation,
    UnixSocketUbuntuLocalAuthenticator,
)
from .ubuntu_worker_ipc import (
    ReconnectingUnixSocketUbuntuWorkerTransport,
    UnixSocketUbuntuWorkerTransport,
)
from .vault_repository import SubprocessVaultRepository
from .windows_worker import OutboundWindowsWorkerTransport, WindowsWorkerRegistration
from .windows_worker_session import WindowsMtlsServerConfig, WindowsWorkerMtlsAcceptor
from .worker_gateway import WorkerGateway, WorkerIdentity


@dataclass(frozen=True, slots=True)
class ServiceRole:
    name: str
    identity: str
    port: int
    operations: tuple[str, ...]


SERVICE_ROLES: Mapping[str, ServiceRole] = {
    role.name: role
    for role in (
        ServiceRole("inbound_receiver", "jarvis-inbound", 9011, ("receive",)),
        ServiceRole("capability_broker", "jarvis-broker", 9012, ("receive",)),
        ServiceRole(
            "orchestration_agent", "jarvis-orchestration", 9013, ("run", "cancel")
        ),
        ServiceRole(
            "audit_service",
            "jarvis-audit",
            9014,
            ("append", "append_batch", "safe_view", "export_json"),
        ),
        ServiceRole(
            "google_connector",
            "jarvis-google",
            9015,
            (
                "gmail_messages_list",
                "gmail_messages_get",
                "gmail_threads_list",
                "gmail_threads_get",
                "calendar_list",
                "calendar_events_list",
                "calendar_events_get",
                "drive_files_list",
                "drive_files_get",
                "drive_files_export",
                "start_authorization",
                "oauth_callback",
                "disconnect",
                "action_bind",
                "action_validate",
                "action_prepare",
                "action_run",
                "action_cancel",
                "action_finalize",
            ),
        ),
        ServiceRole(
            "knowledge_vault_connector",
            "jarvis-vault",
            9016,
            (
                "read",
                "propose",
                "action_bind",
                "action_validate",
                "action_prepare",
                "action_run",
                "action_cancel",
                "action_finalize",
            ),
        ),
        ServiceRole(
            "openwa_outbound_connector",
            "jarvis-openwa-outbound",
            9017,
            ("current", "preflight", "send"),
        ),
        ServiceRole(
            "worker_gateway",
            "jarvis-worker-gateway",
            9018,
            (
                "current",
                "action_prepare",
                "action_run",
                "action_cancel",
                "action_finalize",
            ),
        ),
        ServiceRole(
            "public_oauth_callback", "jarvis-oauth-callback", 8080, ("callback",)
        ),
        ServiceRole(
            "deleted_conversation_archive",
            "jarvis-deleted-archive",
            0,
            (),
        ),
    )
}

_GOOGLE_AUTHORIZATION_ACCESS_SCOPES: Mapping[str, frozenset[str]] = {
    "baseline": frozenset(),
    "gmail-send": frozenset({GMAIL_SEND_SCOPE}),
    "calendar-write": frozenset({CALENDAR_WRITE_SCOPE}),
}


class CompositionError(RuntimeError):
    """A production role could not be assembled from reviewed configuration."""


def _load_configuration(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    if not isinstance(value, dict):
        raise CompositionError("deployment configuration must be a TOML table")
    try:
        validate_configuration(value)
    except BundleValidationError as exc:
        raise CompositionError("active configuration failed validation") from exc
    if value.get("configuration_kind") != "active":
        raise CompositionError("service roots require reviewed active configuration")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise CompositionError("active configuration metadata is unavailable") from exc
    if os.name == "posix" and metadata.st_uid != 0:
        raise CompositionError("active configuration must be owned by root")
    if stat.S_IMODE(metadata.st_mode) != 0o444:
        raise CompositionError("active configuration must have mode 0444")
    return value


def _read_secret(path: Path) -> bytes:
    try:
        metadata = path.stat()
        secret = path.read_bytes()
    except OSError as exc:
        raise CompositionError("service protocol key is unavailable") from exc
    if path.is_symlink():
        raise CompositionError("service protocol key must not be a symbolic link")
    if os.name == "posix" and (
        stat.S_IMODE(metadata.st_mode) != 0o440 or metadata.st_gid != 20000
    ):
        raise CompositionError("service protocol key ownership or mode is invalid")
    if secret.endswith(b"\n"):
        secret = secret[:-1]
    if len(secret) < 32:
        raise CompositionError("service protocol key must contain at least 32 bytes")
    return secret


def _credential_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.stat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompositionError("service credential document is unavailable") from exc
    if path.is_symlink():
        raise CompositionError(
            "service credential document must not be a symbolic link"
        )
    if os.name == "posix" and (
        stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid()
    ):
        raise CompositionError("service credential ownership or mode is invalid")
    if not isinstance(value, dict):
        raise CompositionError("service credential document must be an object")
    return value


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CompositionError(f"{name} must be a non-empty canonical string")
    return value


def _audit_operations(config: Mapping[str, Any]) -> Mapping[str, Callable[..., object]]:
    paths = config.get("paths")
    if not isinstance(paths, Mapping):
        raise CompositionError("configuration paths are unavailable")
    root = Path(_require_text(paths.get("audit"), "paths.audit"))
    root.mkdir(parents=True, exist_ok=True)
    audit = SQLiteAuditBoundary(root / "audit.sqlite3")
    return {
        "append": audit.append,
        "append_batch": audit.append_batch,
        "safe_view": audit.safe_view,
        "export_json": audit.export_json,
    }


def _orchestration_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    credential = _credential_json(Path("/run/credentials/openai/credentials.json"))
    api_key = _require_text(credential.get("api_key"), "OpenAI api_key")
    from agents import RunConfig
    from agents.models.openai_provider import OpenAIProvider

    model_provider = OpenAIProvider(api_key=api_key)
    google = _RemoteGoogleReads(
        _client(
            config,
            client_identity="jarvis-orchestration",
            server_role="google_connector",
        )
    )
    vault_client = _client(
        config,
        client_identity="jarvis-orchestration",
        server_role="knowledge_vault_connector",
    )
    models = config.get("models")
    timeouts = config.get("timeouts")
    if not isinstance(models, Mapping) or not isinstance(timeouts, Mapping):
        raise CompositionError("Codex deployment configuration is incomplete")
    _clock, _ids, trace = _service_trace(
        config, "codex", root=Path("/var/lib/jarvis/codex-traces")
    )
    codex_specialist = CodexSpecialist(
        config=CodexSpecialistConfig(
            workspaces=(
                CodexWorkspace(
                    name="jarvis",
                    host="ubuntu",
                    cwd="/srv/jarvis-workspace",
                ),
            ),
            model=_require_text(models.get("default_model"), "default_model"),
            reasoning=_require_text(
                models.get("default_reasoning"), "default_reasoning"
            ),
            timeout_seconds=float(timeouts.get("codex_seconds", 0)),
        ),
        adapter=CodexCliAdapter(
            executable=Path("/usr/local/bin/codex"), api_key=api_key
        ),
        inspector=GitCodexWorkspaceInspector(),
        trace=trace,
    )

    def read_vault(_request: object, typed_input: object, deadline: float) -> object:
        if not isinstance(typed_input, VaultReadInput):
            raise TypeError("read_knowledge_vault received invalid input")
        payload = vault_client.call(
            "read", typed_input.model_dump(mode="json"), deadline=deadline
        )
        try:
            result = KnowledgeVaultReadResult.model_validate(payload)
        except (TypeError, ValueError):
            raise TypeError("vault service returned invalid read output")

        return result

    orchestration = AgentsSdkOrchestrationAdapter(
        google_read_connector=google,
        vault_read_tool=BoundedReadTool(
            name="read_knowledge_vault",
            description="Read or search the configured knowledge vault locally.",
            input_model=VaultReadInput,
            output_model=KnowledgeVaultReadResult,
            handler=read_vault,  # type: ignore[arg-type]
        ),
        vault_write_enabled=True,
        codex_specialist=codex_specialist,
        model_turn_timeout_seconds=float(timeouts.get("model_turn_seconds", 0)),
        run_config_factory=lambda **kwargs: RunConfig(
            model_provider=model_provider, **kwargs
        ),
    )
    return {"run": orchestration.run, "cancel": orchestration.cancel}


class _RemoteGoogleReads:
    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def gmail_messages_list(self, **kwargs: object) -> object:
        return self._client.call("gmail_messages_list", **kwargs)

    def gmail_messages_get(self, **kwargs: object) -> object:
        return self._client.call("gmail_messages_get", **kwargs)

    def gmail_threads_list(self, **kwargs: object) -> object:
        return self._client.call("gmail_threads_list", **kwargs)

    def gmail_threads_get(self, **kwargs: object) -> object:
        return self._client.call("gmail_threads_get", **kwargs)

    def calendar_list(self, **kwargs: object) -> object:
        return self._client.call("calendar_list", **kwargs)

    def calendar_events_list(self, **kwargs: object) -> object:
        return self._client.call("calendar_events_list", **kwargs)

    def calendar_events_get(self, **kwargs: object) -> object:
        return self._client.call("calendar_events_get", **kwargs)

    def drive_files_list(self, **kwargs: object) -> object:
        return self._client.call("drive_files_list", **kwargs)

    def drive_files_get(self, **kwargs: object) -> object:
        return self._client.call("drive_files_get", **kwargs)

    def drive_files_export(self, **kwargs: object) -> object:
        return self._client.call("drive_files_export", **kwargs)


def _openwa_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    deployment = config.get("deployment")
    if not isinstance(deployment, Mapping):
        raise CompositionError("deployment configuration is unavailable")
    credential = _credential_json(Path("/run/credentials/openwa/credentials.json"))
    connector = OpenWAOutboundConnector(
        config=OpenWAConfig(
            api_base_url=_require_text(
                credential.get("api_base_url"), "OpenWA api_base_url"
            ),
            api_key=_require_text(credential.get("api_key"), "OpenWA api_key"),
            internal_session_id=_require_text(
                deployment.get("openwa_internal_session_id"),
                "openwa_internal_session_id",
            ),
            named_session=_require_text(
                deployment.get("openwa_named_session"), "openwa_named_session"
            ),
            operator_conversation_id=_require_text(
                deployment.get("openwa_operator_conversation_id"),
                "openwa_operator_conversation_id",
            ),
        )
    )
    return {
        "current": connector.readiness.current,
        "preflight": connector.preflight,
        "send": connector.send,
    }


def _operation_timeouts(
    config: Mapping[str, Any], *, server_role: str
) -> Mapping[str, float]:
    configured = config.get("timeouts")
    if not isinstance(configured, Mapping):
        raise CompositionError("service timeout configuration is unavailable")
    try:
        read = float(configured["read_connector_seconds"]) + 5
        side_effect = float(configured["side_effect_connector_seconds"]) + 5
        terminal = float(configured["terminal_seconds"]) + 5
        active = float(configured["active_request_seconds"]) + 5
    except (KeyError, TypeError, ValueError) as exc:
        raise CompositionError("service timeout configuration is invalid") from exc
    role = SERVICE_ROLES[server_role]
    if server_role == "capability_broker":
        return {"receive": active}
    if server_role == "orchestration_agent":
        return {"run": active, "cancel": side_effect}
    if server_role == "worker_gateway":
        return {operation: terminal for operation in role.operations}
    if server_role in {"google_connector", "knowledge_vault_connector"}:
        return {
            operation: (
                read
                if operation == "read"
                or operation.startswith(("gmail_", "calendar_", "drive_"))
                else side_effect
            )
            for operation in role.operations
        }
    return {operation: side_effect for operation in role.operations}


def _client(
    config: Mapping[str, Any], *, client_identity: str, server_role: str
) -> AuthenticatedServiceClient:
    server = SERVICE_ROLES[server_role]
    client_role = next(
        (
            role.name
            for role in SERVICE_ROLES.values()
            if role.identity == client_identity
        ),
        None,
    )
    if client_role is None:
        raise CompositionError("service client identity has no deployment role")
    return AuthenticatedServiceClient(
        identity=client_identity,
        expected_server_identity=server.identity,
        secret=_read_secret(
            Path("/run/protocol") / f"{client_role}--{server_role}.key"
        ),
        host=server_role,
        port=server.port,
        operation_timeouts=_operation_timeouts(config, server_role=server_role),
    )


def _broker_state_store(state_root: Path) -> SQLiteDurableStateStore:
    """Compose durable broker state with the separate write-only archive client."""

    deleted_archive = SQLiteDeletedConversationArchiveWriter(
        "/run/jarvis-deleted/writer.sock",
        authkey=_read_secret(
            Path("/run/protocol")
            / "capability_broker--deleted_conversation_archive.key"
        ),
    )
    return SQLiteDurableStateStore(
        state_root / "state.sqlite3", deleted_archive=deleted_archive
    )


class _AsyncIngressAdmission:
    """Acknowledge durable admission while one background worker drains ingress."""

    def __init__(self, *, receiver: SignedMessageReceiver, state: object) -> None:
        self._receiver = receiver
        self._worker = OpenWAIngressWorker(receiver=receiver, state=state)  # type: ignore[arg-type]
        self._wakeup = Event()
        Thread(target=self._drain, daemon=True).start()

    def receive(self, event: SignedInboundEvent) -> object:
        result = self._receiver.admit(event)
        if result.disposition == "admitted":
            self._wakeup.set()
        return result

    def _drain(self) -> None:
        while True:
            self._wakeup.wait()
            self._wakeup.clear()
            try:
                while self._worker.run_once() is not None:
                    pass
            except Exception:  # noqa: BLE001 - isolate one interrupted ingress item
                self._wakeup.set()


def _broker_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    deployment = config.get("deployment")
    paths = config.get("paths")
    model_config = config.get("models")
    if not isinstance(deployment, Mapping):
        raise CompositionError("broker deployment configuration is incomplete")
    if not isinstance(paths, Mapping):
        raise CompositionError("broker path configuration is incomplete")
    if not isinstance(model_config, Mapping):
        raise CompositionError("broker model configuration is incomplete")

    state_root = Path(_require_text(paths.get("state"), "paths.state"))
    trace_root = Path(_require_text(paths.get("traces"), "paths.traces"))
    state_root.mkdir(parents=True, exist_ok=True)
    trace_root.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    ids = UuidIdGenerator()
    state = _broker_state_store(state_root)
    audit = RemoteAuditBoundary(
        _client(config, client_identity="jarvis-broker", server_role="audit_service")
    )
    orchestration = RemoteOrchestrationAdapter(
        _client(
            config,
            client_identity="jarvis-broker",
            server_role="orchestration_agent",
        )
    )
    outbound_client = _client(
        config, client_identity="jarvis-broker", server_role="openwa_outbound_connector"
    )
    google_actions = RemoteActionDispatcher(
        _client(
            config,
            client_identity="jarvis-broker",
            server_role="google_connector",
        ),
        bound=True,
    )
    vault_client = _client(
        config, client_identity="jarvis-broker", server_role="knowledge_vault_connector"
    )
    vault_actions = RemoteActionDispatcher(vault_client, bound=True)
    worker_client = _client(
        config, client_identity="jarvis-broker", server_role="worker_gateway"
    )
    worker_actions = RemoteActionDispatcher(worker_client)
    actions = RoutedActionDispatcher(
        terminal=worker_actions,
        gmail=google_actions,
        gmail_lifecycle=google_actions,
        calendar=google_actions,
        calendar_lifecycle=google_actions,
        vault=vault_actions,
        vault_lifecycle=vault_actions,
    )
    trace_store = SQLiteDiagnosticTraceStore(
        trace_root / "traces.sqlite3",
        minimum_free_bytes=_minimum_free_bytes(config),
    )
    trace = DiagnosticTraceRecorder(writer=trace_store.writer(), clock=clock, ids=ids)
    allowed_models = model_config.get("allowed_models")
    allowed_reasoning = model_config.get("allowed_reasoning")
    if not isinstance(allowed_models, list) or not isinstance(allowed_reasoning, list):
        raise CompositionError("model availability configuration is invalid")
    broker_credential = _credential_json(
        Path("/run/credentials/broker/credentials.json")
    )
    control_config = ControlPlaneConfig(
        operator_id=_require_text(deployment.get("operator_id"), "operator_id"),
        session_id=_require_text(
            deployment.get("openwa_internal_session_id"),
            "openwa_internal_session_id",
        ),
        signing_secret=_require_text(
            broker_credential.get("openwa_signing_secret"), "OpenWA signing secret"
        ).encode(),
    )
    broker = DeterministicCapabilityBroker(
        config=control_config,
        state=state,
        audit=audit,
        orchestration=orchestration,
        outbound=RemoteOutboundConnector(outbound_client),
        clock=clock,
        ids=ids,
        trace=trace,
        model_availability_provider=FixedModelAvailabilityProvider(
            ModelAvailability(
                available_models=tuple(allowed_models),
                available_reasoning_levels=tuple(allowed_reasoning),
            )
        ),
        messaging_readiness_provider=RemoteMessagingReadinessProvider(outbound_client),
        worker_readiness_provider=RemoteWorkerReadinessProvider(worker_client),
        working_sessions=SQLiteWorkingSessionStore(state_root / "sessions.sqlite3"),
        action_dispatcher=actions,
        action_lifecycle=actions,
        vault_write_proposal_preparer=RemoteVaultProposalPreparer(vault_client),
    )
    receiver = SignedMessageReceiver(
        config=control_config,
        state=state,
        audit=audit,
        broker=broker,
        clock=clock,
        ids=ids,
    )
    ingress = _AsyncIngressAdmission(receiver=receiver, state=state)
    return {"receive": ingress.receive}


class _GoogleActionDispatcher:
    def __init__(
        self, *, gmail: GmailWriteConnector, calendar: CalendarActionDispatcher
    ) -> None:
        self._gmail = gmail
        self._calendar = calendar
        self._owners: dict[str, object] = {}
        self._lock = RLock()

    def _owner(self, action: FrozenActionProposal) -> object:
        if action.kind in {"gmail_send", "gmail_reply"}:
            return self._gmail
        if action.kind in {"calendar_insert", "calendar_update", "calendar_patch"}:
            return self._calendar
        raise ValueError("action kind is outside the Google connector")

    def bind_proposal(self, action: FrozenActionProposal) -> FrozenActionProposal:
        owner = self._owner(action)
        binder = getattr(owner, "bind_proposal", None)
        return binder(action) if callable(binder) else action

    def validate_pending_action(self, action: FrozenActionProposal) -> None:
        owner = self._owner(action)
        validate = getattr(owner, "validate_pending_action", None)
        if callable(validate):
            validate(action)

    def prepare(self, action: FrozenActionProposal) -> object:
        owner = self._owner(action)
        handle = owner.prepare(action)  # type: ignore[attr-defined]
        with self._lock:
            self._owners[action.action_id] = owner
        return handle

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        with self._lock:
            owner = self._owners.get(action_id)
        if owner is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return owner.cancel(action_id=action_id)  # type: ignore[attr-defined,no-any-return]


def _minimum_free_bytes(config: Mapping[str, Any]) -> int:
    bounds = config.get("resource_bounds")
    if not isinstance(bounds, Mapping):
        raise CompositionError("resource bound configuration is unavailable")
    gib = bounds.get("minimum_free_disk_gib")
    if isinstance(gib, bool) or not isinstance(gib, int) or gib < 0:
        raise CompositionError("minimum free disk configuration is invalid")
    return gib * 1024 * 1024 * 1024


def _service_trace(
    config: Mapping[str, Any], name: str, *, root: Path
) -> tuple[SystemClock, UuidIdGenerator, DiagnosticTraceRecorder]:
    clock = SystemClock()
    ids = UuidIdGenerator()
    root.mkdir(parents=True, exist_ok=True)
    store = SQLiteDiagnosticTraceStore(
        root / f"{name}.sqlite3",
        minimum_free_bytes=_minimum_free_bytes(config),
    )
    return (
        clock,
        ids,
        DiagnosticTraceRecorder(writer=store.writer(), clock=clock, ids=ids),
    )


def _google_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    deployment = config.get("deployment")
    if not isinstance(deployment, Mapping):
        raise CompositionError("Google deployment configuration is unavailable")
    identity = _require_text(deployment.get("google_subject"), "google_subject")
    credentials = _credential_json(Path("/run/credentials/google/credentials.json"))
    client_id = _require_text(credentials.get("client_id"), "Google client_id")
    client_secret = _require_text(
        credentials.get("client_secret"), "Google client_secret"
    )
    credential_store = FileGoogleCredentialStore("/run/credentials/google")
    state_store = SQLiteGoogleOAuthStateStore(
        "/run/credentials/google/oauth-state.sqlite3"
    )
    connection = state_store.get_connection()
    if not connection.connected and credential_store.current is not None:
        try:
            credential_store.delete()
        except GoogleOAuthError as exc:
            raise CompositionError(
                "disconnected Google credential could not be discarded"
            ) from exc
    clock, ids, trace = _service_trace(
        config, "google", root=Path("/var/lib/jarvis/google-traces")
    )
    audit = RemoteAuditBoundary(
        _client(config, client_identity="jarvis-google", server_role="audit_service")
    )
    provider = GoogleLiveOAuthProvider(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=_require_text(
            deployment.get("oauth_callback_url"), "oauth_callback_url"
        ),
    )
    lifecycle = GoogleOAuthLifecycle(
        configured_identity=identity,
        state_store=state_store,
        credential_store=credential_store,
        provider=provider,
        audit=audit,
        trace=trace,
        clock=clock,
        ids=ids,
    )
    reads = GoogleReadConnector(
        configured_identity=identity,
        credential_store=credential_store,
        provider=GoogleApiReadProvider(
            client_id=client_id, client_secret=client_secret
        ),
        audit=audit,
        trace=trace,
        clock=clock,
        ids=ids,
        on_invalid_grant=lambda generation: lifecycle.handle_refresh_failure(
            "invalid_grant", connection_generation=generation
        ),
    )
    gmail = GmailWriteConnector(
        configured_identity=identity,
        credential_store=credential_store,
        provider=GmailApiWriteProvider(
            client_id=client_id, client_secret=client_secret
        ),
        audit=audit,
        trace=trace,
        clock=clock,
        ids=ids,
        connection_binding=lifecycle.connection_binding,
        on_invalid_grant=lambda: lifecycle.handle_refresh_failure("invalid_grant"),
    )
    calendar = CalendarActionDispatcher(
        configured_identity=identity,
        connection_state=state_store,
        credential_store=credential_store,
        provider=GoogleApiCalendarWriteProvider(
            client_id=client_id, client_secret=client_secret
        ),
        trace=trace,
        on_invalid_grant=lambda generation: lifecycle.handle_refresh_failure(
            "invalid_grant", connection_generation=generation
        ),
    )
    operations = {
        name: getattr(reads, name)
        for name in SERVICE_ROLES["google_connector"].operations
        if name.startswith(("gmail_", "calendar_", "drive_"))
    }
    operations.update(
        OwnedActionService(
            _GoogleActionDispatcher(gmail=gmail, calendar=calendar)  # type: ignore[arg-type]
        ).operations()
    )

    def start_authorization(
        *, operation_id: str, requested_scopes: Sequence[str]
    ) -> str:
        authorization = lifecycle.start_authorization(
            operation_id=operation_id,
            requested_scopes=(*requested_scopes, "openid"),
        )
        return provider.authorization_url(authorization)

    operations.update(
        {
            "start_authorization": start_authorization,
            "oauth_callback": lifecycle.handle_callback,
            "disconnect": lifecycle.disconnect,
        }
    )
    return operations


def _vault_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    deployment = config.get("deployment")
    if not isinstance(deployment, Mapping):
        raise CompositionError("vault deployment configuration is unavailable")
    root = Path("/var/lib/jarvis/vault")
    if not root.is_dir():
        raise CompositionError("knowledge-vault clone is unavailable")
    repository = SubprocessVaultRepository(
        ssh_executable=Path("/usr/bin/ssh"),
        ssh_config_path=Path("/run/credentials/vault/ssh_config"),
        known_hosts_path=Path("/run/credentials/vault/known_hosts"),
        proxy_command=(
            "/usr/local/bin/python",
            "-m",
            "jarvis_control_plane.service_runtime",
            "egress-connect",
            "%h",
            "%p",
            "--proxy-host",
            "vault_egress_proxy",
        ),
    )
    repository.validate_remote(
        root,
        _require_text(deployment.get("vault_remote"), "vault_remote"),
    )
    clock = SystemClock()
    reads = KnowledgeVaultConnector(root=root, synchronizer=repository, now=clock.now)
    note_directories = deployment.get("vault_note_directories")
    if not isinstance(note_directories, list) or not all(
        isinstance(item, str) for item in note_directories
    ):
        raise CompositionError("vault note directories are invalid")
    writes = KnowledgeVaultWriteConnector(
        root=root,
        repository=repository,
        now=clock.now,
        allowed_note_directories=tuple(note_directories),
    )
    operations: dict[str, Callable[..., object]] = {
        "read": lambda payload, deadline=None: reads.read(
            VaultReadInput.model_validate(payload), deadline=deadline
        ).model_dump(mode="json"),
        "propose": writes.propose,
    }
    operations.update(OwnedActionService(writes).operations())
    return operations


def _worker_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    deployment = config.get("deployment")
    paths = config.get("paths")
    if not isinstance(deployment, Mapping) or not isinstance(paths, Mapping):
        raise CompositionError("worker deployment configuration is unavailable")
    credentials = _credential_json(
        Path("/run/credentials/windows-worker/credentials.json")
    )
    socket_path = _require_text(
        paths.get("ubuntu_worker_socket"), "ubuntu_worker_socket"
    )
    ubuntu_identity = WorkerIdentity(
        host="ubuntu",
        worker_id=_require_text(
            deployment.get("ubuntu_worker_identity"), "ubuntu_worker_identity"
        ),
        connection_id=_require_text(
            credentials.get("ubuntu_connection_id"), "ubuntu_connection_id"
        ),
    )
    ubuntu_peer = UbuntuLocalPeerExpectation(
        peer_uid=int(credentials.get("ubuntu_peer_uid")),
        socket_owner_uid=int(credentials.get("ubuntu_socket_owner_uid")),
        socket_path=socket_path,
    )

    def connect_ubuntu() -> UnixSocketUbuntuWorkerTransport:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(socket_path)
            return UnixSocketUbuntuWorkerTransport(
                connection=connection,
                authenticator=UnixSocketUbuntuLocalAuthenticator(
                    connection=connection,
                    socket_path=socket_path,
                    connection_id=ubuntu_identity.connection_id,
                ),
                expected_peer=ubuntu_peer,
                registered_identity=ubuntu_identity,
            )
        except BaseException:
            connection.close()
            raise

    try:
        initial_ubuntu = connect_ubuntu()
    except OSError as exc:
        raise CompositionError("native Ubuntu worker socket is unavailable") from exc
    ubuntu = ReconnectingUnixSocketUbuntuWorkerTransport(
        connect=connect_ubuntu,
        initial=initial_ubuntu,
    )
    windows_identity = WorkerIdentity(
        host="windows",
        worker_id=_require_text(
            deployment.get("windows_worker_identity"), "windows_worker_identity"
        ),
        connection_id=_require_text(
            credentials.get("windows_connection_id"), "windows_connection_id"
        ),
    )
    registration = WindowsWorkerRegistration(
        identity=windows_identity,
        certificate_identity=_require_text(
            credentials.get("windows_certificate_identity"),
            "windows_certificate_identity",
        ),
        application_identity=_require_text(
            credentials.get("windows_application_identity"),
            "windows_application_identity",
        ),
    )
    windows = OutboundWindowsWorkerTransport(registration=registration)
    acceptor = WindowsWorkerMtlsAcceptor(
        config=WindowsMtlsServerConfig(
            bind_host=_require_text(
                credentials.get("windows_overlay_bind_host"),
                "windows_overlay_bind_host",
            ),
            bind_port=int(credentials.get("windows_overlay_bind_port")),
            ca_file=Path("/run/credentials/windows-worker/worker-ca.pem"),
            certificate_file=Path(
                "/run/credentials/windows-worker/gateway-certificate.pem"
            ),
            private_key_file=Path(
                "/run/credentials/windows-worker/gateway-private-key.pem"
            ),
        ),
        registration=registration,
        transport=windows,
    )
    acceptor.start()
    gateway = WorkerGateway(
        workers={"ubuntu": ubuntu, "windows": windows},
        registered_identities={
            "ubuntu": ubuntu_identity,
            "windows": windows_identity,
        },
    )
    operations = dict(OwnedActionService(gateway).operations())
    operations["current"] = gateway.current
    return operations


_ROOTS: Mapping[
    str, Callable[[Mapping[str, Any]], Mapping[str, Callable[..., object]]]
] = {
    "audit_service": _audit_operations,
    "capability_broker": _broker_operations,
    "google_connector": _google_operations,
    "knowledge_vault_connector": _vault_operations,
    "worker_gateway": _worker_operations,
    "orchestration_agent": _orchestration_operations,
    "openwa_outbound_connector": _openwa_operations,
}


def build_operations(
    role_name: str, configuration: Mapping[str, Any]
) -> Mapping[str, Callable[..., object]]:
    """Assemble one role; missing roots fail closed instead of feigning readiness."""

    role = SERVICE_ROLES.get(role_name)
    if role is None:
        raise CompositionError("unknown service role")
    factory = _ROOTS.get(role_name)
    if factory is None:
        raise CompositionError(f"composition root is not implemented for {role_name}")
    operations = factory(configuration)
    if set(operations) != set(role.operations):
        raise CompositionError(f"composition root operations differ for {role_name}")
    return operations


def _verified_inbound_event(
    raw_body: bytes, signature: str | None, signing_secret: bytes
) -> SignedInboundEvent | None:
    event = SignedInboundEvent(raw_body=raw_body, signature=signature)
    return event if event.verify(signing_secret) else None


def _serve_inbound_receiver(config: Mapping[str, Any], protocol_root: Path) -> None:
    credential = _credential_json(
        Path("/run/credentials/openwa-inbound/credentials.json")
    )
    signing_secret = _require_text(
        credential.get("openwa_signing_secret"), "OpenWA signing secret"
    ).encode()
    broker = AuthenticatedServiceClient(
        identity="jarvis-inbound",
        expected_server_identity="jarvis-broker",
        secret=_read_secret(protocol_root / "inbound_receiver--capability_broker.key"),
        host="capability_broker",
        port=SERVICE_ROLES["capability_broker"].port,
        operation_timeouts=_operation_timeouts(config, server_role="capability_broker"),
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisInboundReceiver/1"

        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self) -> None:
            if self.path != "/webhook":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 128 * 1024:
                self.send_error(413)
                return
            signatures = self.headers.get_all("X-OpenWA-Signature") or []
            signature = signatures[0] if len(signatures) == 1 else None
            raw_body = self.rfile.read(length)
            event = _verified_inbound_event(raw_body, signature, signing_secret)
            if event is None:
                payload = b'{"disposition":"unauthenticated"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            try:
                result = broker.call(
                    "receive",
                    event,
                )
                status = getattr(result, "status_code", 503)
                payload = json.dumps(
                    {
                        "disposition": getattr(result, "disposition", "unavailable"),
                        "reason": getattr(result, "reason", None),
                    },
                    separators=(",", ":"),
                ).encode()
            except Exception:  # noqa: BLE001 - private upstream fails closed
                status = 503
                payload = b'{"disposition":"unavailable"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(
        ("0.0.0.0", SERVICE_ROLES["inbound_receiver"].port), Handler
    )
    server.daemon_threads = True
    server.serve_forever()


def _serve_oauth_callback(config: Mapping[str, Any], protocol_root: Path) -> None:
    google = AuthenticatedServiceClient(
        identity="jarvis-oauth-callback",
        expected_server_identity="jarvis-google",
        secret=_read_secret(
            protocol_root / "public_oauth_callback--google_connector.key"
        ),
        host="google_connector",
        port=SERVICE_ROLES["google_connector"].port,
        operation_timeouts=_operation_timeouts(config, server_role="google_connector"),
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisOAuthCallback/1"

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            if parsed.path != "/callback":
                self.send_error(404)
                return
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            if len({name for name, _value in pairs}) != len(pairs):
                self._empty_response(400)
                return
            try:
                result = google.call("oauth_callback", method="GET", query=dict(pairs))
                status = getattr(result, "status_code", 503)
                headers = getattr(result, "headers", {})
            except Exception:  # noqa: BLE001 - private upstream fails closed
                status = 503
                headers = {}
            self.send_response(status)
            for name in ("Cache-Control", "Referrer-Policy"):
                value = headers.get(name) if isinstance(headers, Mapping) else None
                if isinstance(value, str):
                    self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            self._empty_response(405)

        def _empty_response(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(
        ("0.0.0.0", SERVICE_ROLES["public_oauth_callback"].port), Handler
    )
    server.daemon_threads = True
    server.serve_forever()


def serve(role_name: str, *, configuration_path: Path, protocol_root: Path) -> None:
    role = SERVICE_ROLES.get(role_name)
    if role is None:
        raise CompositionError("unknown service role")
    expected_identity = os.environ.get("JARVIS_SERVICE_IDENTITY")
    if expected_identity != role.identity:
        raise CompositionError("runtime service identity does not match its role")
    configuration = _load_configuration(configuration_path)
    identities = configuration.get("identities")
    if (
        not isinstance(identities, Mapping)
        or identities.get(role_name) != role.identity
    ):
        raise CompositionError("configured service identity does not match its role")
    if role_name == "inbound_receiver":
        _serve_inbound_receiver(configuration, protocol_root)
        return
    if role_name == "public_oauth_callback":
        _serve_oauth_callback(configuration, protocol_root)
        return
    if role_name == "deleted_conversation_archive":
        paths = configuration.get("paths")
        if not isinstance(paths, Mapping):
            raise CompositionError("deleted archive configuration is incomplete")
        archive_root = Path(
            _require_text(
                paths.get("deleted_conversations"), "paths.deleted_conversations"
            )
        )
        archive_root.mkdir(parents=True, exist_ok=True)
        serve_sqlite_deleted_conversation_archive(
            archive_root / "deleted-conversations.sqlite3",
            "/run/jarvis-deleted/writer.sock",
            authkey=_read_secret(
                protocol_root / "capability_broker--deleted_conversation_archive.key"
            ),
        )
        return
    operations = build_operations(role_name, configuration)
    allowed_identities, operation_allowlists = _service_access(role_name, role)
    client_secrets: dict[str, bytes] = {}
    for client_identity in allowed_identities:
        client_role = next(
            item.name
            for item in SERVICE_ROLES.values()
            if item.identity == client_identity
        )
        client_secrets[client_identity] = _read_secret(
            protocol_root / f"{client_role}--{role_name}.key"
        )
    AuthenticatedServiceServer(
        identity=role.identity,
        client_secrets=client_secrets,
        host="0.0.0.0",
        port=role.port,
        operations=operations,
        allowed_client_identities=allowed_identities,
        allowed_operations_by_client=operation_allowlists,
    ).serve_forever()


def _service_access(
    role_name: str, role: ServiceRole
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
            "jarvis-broker": ("append", "append_batch"),
            "jarvis-google": ("append", "append_batch"),
        }
    elif role_name == "google_connector":
        operation_allowlists = {
            "jarvis-broker": tuple(
                operation
                for operation in role.operations
                if operation.startswith("action_")
                or operation in {"start_authorization", "disconnect"}
            ),
            "jarvis-orchestration": tuple(
                operation
                for operation in role.operations
                if operation.startswith(("gmail_", "calendar_", "drive_"))
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


def health() -> None:
    identity = os.environ.get("JARVIS_SERVICE_IDENTITY")
    role = next(
        (item for item in SERVICE_ROLES.values() if item.identity == identity), None
    )
    if role is None:
        raise CompositionError("runtime service identity is unknown")
    if role.name == "deleted_conversation_archive":
        writer = SQLiteDeletedConversationArchiveWriter(
            "/run/jarvis-deleted/writer.sock",
            authkey=_read_secret(
                Path("/run/protocol")
                / "capability_broker--deleted_conversation_archive.key"
            ),
        )
        writer.close()
        return
    try:
        with urlopen(f"http://127.0.0.1:{role.port}/health", timeout=2) as response:
            if response.status != 200 or response.read(3) != b"ok":
                raise CompositionError("service health response is invalid")
    except (OSError, URLError) as exc:
        raise CompositionError("service is not healthy") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve_parser = subcommands.add_parser("serve")
    serve_parser.add_argument("role", choices=tuple(SERVICE_ROLES))
    serve_parser.add_argument(
        "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
    )
    serve_parser.add_argument(
        "--protocol-root", type=Path, default=Path("/run/protocol")
    )
    subcommands.add_parser("health")
    proxy_parser = subcommands.add_parser("serve-egress-proxy")
    proxy_parser.add_argument("kind", choices=("orchestration", "google", "vault"))
    proxy_parser.add_argument("--port", type=int, default=9080)
    proxy_parser.add_argument(
        "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
    )
    proxy_health = subcommands.add_parser("proxy-health")
    proxy_health.add_argument("--port", type=int, default=9080)
    connect_parser = subcommands.add_parser("egress-connect")
    connect_parser.add_argument("target_host")
    connect_parser.add_argument("target_port", type=int)
    connect_parser.add_argument("--proxy-host", required=True)
    connect_parser.add_argument("--proxy-port", type=int, default=9080)
    authorize_parser = subcommands.add_parser("google-authorize")
    authorize_parser.add_argument("--operation-id", required=True)
    authorize_parser.add_argument(
        "--access",
        choices=tuple(_GOOGLE_AUTHORIZATION_ACCESS_SCOPES),
        default="baseline",
    )
    authorize_parser.add_argument(
        "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
    )
    disconnect_parser = subcommands.add_parser("google-disconnect")
    disconnect_parser.add_argument(
        "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
    )
    for command in ("audit-view", "audit-export"):
        audit_parser = subcommands.add_parser(command)
        audit_parser.add_argument(
            "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "serve-egress-proxy":
        expected_identity = f"jarvis-{arguments.kind}-egress"
        if os.environ.get("JARVIS_SERVICE_IDENTITY") != expected_identity:
            raise CompositionError("egress proxy identity does not match its role")
        configuration = _load_configuration(arguments.configuration)
        egress = configuration.get("egress")
        if not isinstance(egress, Mapping):
            raise CompositionError("egress proxy configuration is unavailable")
        hosts = egress.get(f"{arguments.kind}_hosts")
        if not isinstance(hosts, list) or not all(
            isinstance(item, str) for item in hosts
        ):
            raise CompositionError("egress proxy host allowlist is invalid")
        serve_egress_proxy(
            host="0.0.0.0",
            port=arguments.port,
            allowed_hosts=hosts,
            allowed_ports=(22,) if arguments.kind == "vault" else (443,),
        )
    elif arguments.command == "proxy-health":
        try:
            with socket.create_connection(
                ("127.0.0.1", arguments.port), timeout=2
            ) as probe:
                probe.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
                response = probe.recv(128)
            if not response.startswith(b"HTTP/1.1 200 "):
                raise CompositionError("egress proxy health response is invalid")
        except OSError as exc:
            raise CompositionError("egress proxy is not healthy") from exc
    elif arguments.command == "egress-connect":
        connect_through_proxy(
            proxy_host=arguments.proxy_host,
            proxy_port=arguments.proxy_port,
            target_host=arguments.target_host,
            target_port=arguments.target_port,
        )
    elif arguments.command == "health":
        health()
    elif arguments.command in {"audit-view", "audit-export"}:
        if os.environ.get("JARVIS_SERVICE_IDENTITY") != "jarvis-audit":
            raise CompositionError("audit administration requires the audit identity")
        configuration = _load_configuration(arguments.configuration)
        paths = configuration.get("paths")
        if not isinstance(paths, Mapping):
            raise CompositionError("audit path configuration is unavailable")
        audit_root = Path(_require_text(paths.get("audit"), "paths.audit"))
        audit = SQLiteAuditBoundary(audit_root / "audit.sqlite3")
        if arguments.command == "audit-view":
            print(json.dumps(_encode(audit.safe_view()), separators=(",", ":")))
        else:
            print(audit.export_json())
    elif arguments.command in {"google-authorize", "google-disconnect"}:
        if os.environ.get("JARVIS_SERVICE_IDENTITY") != "jarvis-broker":
            raise CompositionError("Google administration requires the broker identity")
        configuration = _load_configuration(arguments.configuration)
        client = _client(
            configuration,
            client_identity="jarvis-broker",
            server_role="google_connector",
        )
        if arguments.command == "google-authorize":
            requested_scopes = (
                GOOGLE_OAUTH_BASELINE_SCOPES
                | (_GOOGLE_AUTHORIZATION_ACCESS_SCOPES[arguments.access])
            )
            result = client.call(
                "start_authorization",
                operation_id=arguments.operation_id,
                requested_scopes=tuple(sorted(requested_scopes)),
            )
            if not isinstance(result, str) or not result.startswith(
                "https://accounts.google.com/"
            ):
                raise CompositionError("Google authorization URL was unavailable")
            print(result)
        else:
            client.call("disconnect")
            print("Google connection disconnected.")
    else:
        serve(
            arguments.role,
            configuration_path=arguments.configuration,
            protocol_root=arguments.protocol_root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
