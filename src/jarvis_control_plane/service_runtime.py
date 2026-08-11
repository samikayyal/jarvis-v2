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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
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
from .control_plane import (
    ControlPlaneConfig,
    DeterministicCapabilityBroker,
    SignedMessageReceiver,
)
from .deployment import BundleValidationError, validate_configuration
from .gmail_writes import GmailApiWriteProvider, GmailWriteConnector
from .google_calendar import (
    CalendarActionDispatcher,
    GoogleApiCalendarWriteProvider,
)
from .google_oauth import (
    FileGoogleCredentialStore,
    GoogleLiveOAuthProvider,
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
from .openwa import OpenWAConfig, OpenWAOutboundConnector
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
)
from .sessions import ModelAvailability, SQLiteWorkingSessionStore
from .traces import DiagnosticTraceRecorder, SQLiteDiagnosticTraceStore
from .ubuntu_worker import (
    UbuntuLocalPeerExpectation,
    UnixSocketUbuntuLocalAuthenticator,
)
from .ubuntu_worker_ipc import UnixSocketUbuntuWorkerTransport
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
        ServiceRole("orchestration_agent", "jarvis-orchestration", 9013, ("run",)),
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
            ("action_prepare", "action_run", "action_cancel", "action_finalize"),
        ),
        ServiceRole(
            "public_oauth_callback", "jarvis-oauth-callback", 8080, ("callback",)
        ),
    )
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
    _config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    credential = _credential_json(Path("/run/credentials/openai/credentials.json"))
    api_key = _require_text(credential.get("api_key"), "OpenAI api_key")
    from agents import RunConfig
    from agents.models.openai_provider import OpenAIProvider

    model_provider = OpenAIProvider(api_key=api_key)
    google = _RemoteGoogleReads(
        _client(client_identity="jarvis-orchestration", server_role="google_connector")
    )
    vault_client = _client(
        client_identity="jarvis-orchestration",
        server_role="knowledge_vault_connector",
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
        run_config_factory=lambda **kwargs: RunConfig(
            model_provider=model_provider, **kwargs
        ),
    )
    return {"run": orchestration.run}


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


def _client(*, client_identity: str, server_role: str) -> AuthenticatedServiceClient:
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
    )


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
    state = SQLiteDurableStateStore(state_root / "state.sqlite3")
    audit = RemoteAuditBoundary(
        _client(client_identity="jarvis-broker", server_role="audit_service")
    )
    orchestration = RemoteOrchestrationAdapter(
        _client(client_identity="jarvis-broker", server_role="orchestration_agent")
    )
    outbound_client = _client(
        client_identity="jarvis-broker", server_role="openwa_outbound_connector"
    )
    google_actions = RemoteActionDispatcher(
        _client(client_identity="jarvis-broker", server_role="google_connector"),
        bound=True,
    )
    vault_client = _client(
        client_identity="jarvis-broker", server_role="knowledge_vault_connector"
    )
    vault_actions = RemoteActionDispatcher(vault_client, bound=True)
    worker_actions = RemoteActionDispatcher(
        _client(client_identity="jarvis-broker", server_role="worker_gateway")
    )
    actions = RoutedActionDispatcher(
        terminal=worker_actions,
        gmail=google_actions,
        gmail_lifecycle=google_actions,
        calendar=google_actions,
        calendar_lifecycle=google_actions,
        vault=vault_actions,
        vault_lifecycle=vault_actions,
    )
    trace_store = SQLiteDiagnosticTraceStore(trace_root / "traces.sqlite3")
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
    return {"receive": receiver.receive}


class _GoogleActionDispatcher:
    def __init__(
        self, *, gmail: GmailWriteConnector, calendar: CalendarActionDispatcher
    ) -> None:
        self._gmail = gmail
        self._calendar = calendar
        self._owners: dict[str, object] = {}

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
        self._owners[action.action_id] = owner
        return handle

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        owner = self._owners.get(action_id)
        if owner is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return owner.cancel(action_id=action_id)  # type: ignore[attr-defined,no-any-return]


def _service_trace(
    name: str, *, root: Path
) -> tuple[SystemClock, UuidIdGenerator, DiagnosticTraceRecorder]:
    clock = SystemClock()
    ids = UuidIdGenerator()
    root.mkdir(parents=True, exist_ok=True)
    store = SQLiteDiagnosticTraceStore(root / f"{name}.sqlite3")
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
    credential = credential_store.current
    if credential is not None and not state_store.get_connection().connected:
        state_store.set_connection(
            connected=True, granted_scopes=credential.granted_scopes
        )

    clock, ids, trace = _service_trace(
        "google", root=Path("/var/lib/jarvis/google-traces")
    )
    audit = RemoteAuditBoundary(
        _client(client_identity="jarvis-google", server_role="audit_service")
    )
    lifecycle = GoogleOAuthLifecycle(
        configured_identity=identity,
        state_store=state_store,
        credential_store=credential_store,
        provider=GoogleLiveOAuthProvider(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=_require_text(
                deployment.get("oauth_callback_url"), "oauth_callback_url"
            ),
        ),
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
    operations.update(
        {
            "start_authorization": lifecycle.start_authorization,
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
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(socket_path)
    except OSError as exc:
        connection.close()
        raise CompositionError("native Ubuntu worker socket is unavailable") from exc
    ubuntu_identity = WorkerIdentity(
        host="ubuntu",
        worker_id=_require_text(
            deployment.get("ubuntu_worker_identity"), "ubuntu_worker_identity"
        ),
        connection_id=_require_text(
            credentials.get("ubuntu_connection_id"), "ubuntu_connection_id"
        ),
    )
    authenticator = UnixSocketUbuntuLocalAuthenticator(
        connection=connection,
        socket_path=socket_path,
        connection_id=ubuntu_identity.connection_id,
    )
    ubuntu = UnixSocketUbuntuWorkerTransport(
        connection=connection,
        authenticator=authenticator,
        expected_peer=UbuntuLocalPeerExpectation(
            peer_uid=int(credentials.get("ubuntu_peer_uid")),
            socket_owner_uid=int(credentials.get("ubuntu_socket_owner_uid")),
            socket_path=socket_path,
        ),
        registered_identity=ubuntu_identity,
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
    return OwnedActionService(gateway).operations()


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


def _serve_inbound_receiver(protocol_root: Path) -> None:
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

    HTTPServer(
        ("0.0.0.0", SERVICE_ROLES["inbound_receiver"].port), Handler
    ).serve_forever()


def _serve_oauth_callback(protocol_root: Path) -> None:
    google = AuthenticatedServiceClient(
        identity="jarvis-oauth-callback",
        expected_server_identity="jarvis-google",
        secret=_read_secret(
            protocol_root / "public_oauth_callback--google_connector.key"
        ),
        host="google_connector",
        port=SERVICE_ROLES["google_connector"].port,
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

    HTTPServer(
        ("0.0.0.0", SERVICE_ROLES["public_oauth_callback"].port), Handler
    ).serve_forever()


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
        _serve_inbound_receiver(protocol_root)
        return
    if role_name == "public_oauth_callback":
        _serve_oauth_callback(protocol_root)
        return
    operations = build_operations(role_name, configuration)
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
    AuthenticatedServiceServer(
        identity=role.identity,
        client_secrets=client_secrets,
        host="0.0.0.0",
        port=role.port,
        operations=operations,
        allowed_client_identities=allowed_identities,
        allowed_operations_by_client=operation_allowlists,
    ).serve_forever()


def health() -> None:
    identity = os.environ.get("JARVIS_SERVICE_IDENTITY")
    role = next(
        (item for item in SERVICE_ROLES.values() if item.identity == identity), None
    )
    if role is None:
        raise CompositionError("runtime service identity is unknown")
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "health":
        health()
    else:
        serve(
            arguments.role,
            configuration_path=arguments.configuration,
            protocol_root=arguments.protocol_root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
