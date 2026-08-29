"""Role-specific composition roots for the unactivated deployment bundle."""

from __future__ import annotations

import argparse
import json  # noqa: F401
import os  # noqa: F401
import shutil  # noqa: F401
import socket  # noqa: F401
import stat  # noqa: F401
import sys
import tomllib  # noqa: F401
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: F401
from pathlib import Path
from queue import Queue  # noqa: F401
from threading import Event, RLock, Thread  # noqa: F401
from time import sleep  # noqa: F401
from typing import Any
from urllib.error import URLError  # noqa: F401
from urllib.parse import parse_qsl, urlsplit  # noqa: F401
from urllib.request import urlopen  # noqa: F401

from ..acceptance_failpoints import (
    ReviewedPostDispatchFailpoint,
    reviewed_post_dispatch_failpoint_from_config,
)
from ..action_dispatch import RoutedActionDispatcher  # noqa: F401
from ..adapters import (  # noqa: F401
    FixedModelAvailabilityProvider,
    SQLiteAuditBoundary,
    SQLiteDurableStateStore,
    SystemClock,
    UuidIdGenerator,
)
from ..control_grammar import ControlCommand, parse_control  # noqa: F401
from ..control_plane import (  # noqa: F401
    ControlPlaneConfig,
    DeterministicCapabilityBroker,
    SignedMessageReceiver,
)
from ..conversation_archive import (  # noqa: F401
    SQLiteDeletedConversationArchiveWriter,
    serve_sqlite_deleted_conversation_archive,
)
from ..gmail_actions import GMAIL_SEND_SCOPE  # noqa: F401
from ..gmail_writes import GmailApiWriteProvider, GmailWriteConnector  # noqa: F401
from ..google_oauth import (  # noqa: F401
    GOOGLE_OAUTH_BASELINE_SCOPES,
    FileGoogleCredentialStore,
    GoogleLiveOAuthProvider,
    GoogleOAuthError,
    GoogleOAuthLifecycle,
    SQLiteGoogleOAuthStateStore,
)
from ..google_reads import GoogleApiReadProvider, GoogleReadConnector  # noqa: F401
from ..knowledge_vault import (  # noqa: F401
    KnowledgeVaultConnector,
    KnowledgeVaultReadResult,
    VaultReadInput,
)
from ..knowledge_vault_writes import KnowledgeVaultWriteConnector  # noqa: F401
from ..models import (  # noqa: F401
    FrozenActionProposal,
    InboundMessage,
    SignedInboundEvent,
)
from ..openwa import (  # noqa: F401
    OpenWAConfig,
    OpenWAIngressWorker,
    OpenWAOutboundConnector,
)
from ..orchestration import (  # noqa: F401
    AgentsSdkOrchestrationAdapter,
    BoundedReadTool,
)
from ..ports import (  # noqa: F401
    ActionCancellationResult,
    ActionCancellationStatus,
    DurableStateStore,
)
from ..sessions import ModelAvailability, SQLiteWorkingSessionStore  # noqa: F401
from ..traces import DiagnosticTraceRecorder, SQLiteDiagnosticTraceStore
from ..ubuntu_worker import (  # noqa: F401
    UbuntuLocalPeerExpectation,
    UnixSocketUbuntuLocalAuthenticator,
)
from ..ubuntu_worker_ipc import (  # noqa: F401
    ReconnectingUnixSocketUbuntuWorkerTransport,
    UnixSocketUbuntuWorkerTransport,
)
from ..vault_repository import SubprocessVaultRepository  # noqa: F401
from ..windows_worker import (  # noqa: F401
    OutboundWindowsWorkerTransport,
    WindowsWorkerRegistration,
)
from ..windows_worker_session import (  # noqa: F401
    WindowsMtlsServerConfig,
    WindowsWorkerMtlsAcceptor,
)
from ..worker_gateway import (  # noqa: F401
    WorkerExecutionLimits,
    WorkerGateway,
    WorkerIdentity,
)
from . import service_runtime_admin as _admin
from . import service_runtime_broker as _broker
from . import service_runtime_cli as _cli
from . import service_runtime_connectors as _connectors
from . import service_runtime_http as _http
from . import service_runtime_orchestration as _orchestration
from .deployment import BundleValidationError, validate_configuration  # noqa: F401
from .egress_proxy import connect_through_proxy, serve_egress_proxy  # noqa: F401
from .service_protocol import (  # noqa: F401
    AuthenticatedServiceClient,
    AuthenticatedServiceServer,
    OwnedActionService,
    RemoteActionDispatcher,
    RemoteAuditBoundary,
    RemoteGoogleReadinessProvider,
    RemoteMessagingReadinessProvider,
    RemoteOrchestrationAdapter,
    RemoteOutboundConnector,
    RemoteVaultProposalPreparer,
    RemoteWorkerReadinessProvider,
    ServiceProtocolError,
    _encode,
)
from .service_runtime_config import (
    _GOOGLE_AUTHORIZATION_ACCESS_SCOPES,  # noqa: F401
    SERVICE_ROLES,
    CompositionError,
    ServiceRole,
)
from .service_runtime_config import credential_json as _credential_json  # noqa: F401
from .service_runtime_config import (
    load_configuration as _load_configuration,  # noqa: F401
)
from .service_runtime_config import make_client as _make_client
from .service_runtime_config import minimum_free_bytes as _minimum_free_bytes
from .service_runtime_config import operation_timeouts as _configured_operation_timeouts
from .service_runtime_config import private_key_path as _private_key_path  # noqa: F401
from .service_runtime_config import read_secret as _read_secret
from .service_runtime_config import require_text as _require_text  # noqa: F401
from .service_runtime_config import (
    reviewed_windows_overlay_port as _reviewed_windows_overlay_port,  # noqa: F401
)
from .service_runtime_config import (
    vault_write_timeout as _configured_vault_write_timeout,
)


def _operation_timeouts(
    config: Mapping[str, Any], *, server_role: str
) -> Mapping[str, float]:
    return _configured_operation_timeouts(
        config, server_role=server_role, service_roles=SERVICE_ROLES
    )


def _vault_write_timeout(config: Mapping[str, Any]) -> float:
    return _configured_vault_write_timeout(config)


def _client(
    config: Mapping[str, Any], *, client_identity: str, server_role: str
) -> AuthenticatedServiceClient:
    return _make_client(
        config,
        client_identity=client_identity,
        server_role=server_role,
        service_roles=SERVICE_ROLES,
        read_secret=_read_secret,
        operation_timeouts=_operation_timeouts,
        client_factory=AuthenticatedServiceClient,
    )


def _reviewed_acceptance_failpoint(
    config: Mapping[str, Any],
    *,
    durable_root: Path | str | None = None,
) -> ReviewedPostDispatchFailpoint | None:
    """Load the optional host-reviewed, one-shot Google fault injection."""

    try:
        return reviewed_post_dispatch_failpoint_from_config(
            config.get("acceptance_failpoint"), durable_root=durable_root
        )
    except (TypeError, ValueError) as exc:
        raise CompositionError("acceptance failpoint configuration is invalid") from exc


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


class _AsyncIngressAdmission(_broker.AsyncIngressAdmission):
    def __init__(
        self, *, receiver: SignedMessageReceiver, state: DurableStateStore
    ) -> None:
        super().__init__(receiver=receiver, state=state, runtime=sys.modules[__name__])


_GoogleActionDispatcher = _connectors.GoogleActionDispatcher
_RemoteGoogleReads = _orchestration.RemoteGoogleReads


def _broker_state_store(state_root: Path) -> SQLiteDurableStateStore:
    return _broker.broker_state_store(state_root, runtime=sys.modules[__name__])


def _audit_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    return _broker.audit_operations(config, runtime=sys.modules[__name__])


def _broker_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    return _broker.broker_operations(config, runtime=sys.modules[__name__])


def _orchestration_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    return _orchestration.orchestration_operations(
        config, runtime=sys.modules[__name__]
    )


def _openwa_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    return _orchestration.openwa_operations(config, runtime=sys.modules[__name__])


def _google_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    return _connectors.google_operations(config, runtime=sys.modules[__name__])


def _vault_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    return _connectors.vault_operations(config, runtime=sys.modules[__name__])


def _worker_operations(
    config: Mapping[str, Any],
) -> Mapping[str, Callable[..., object]]:
    return _connectors.worker_operations(config, runtime=sys.modules[__name__])


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
    return _http.verified_inbound_event(raw_body, signature, signing_secret)


def _serve_inbound_receiver(config: Mapping[str, Any], protocol_root: Path) -> None:
    _http.serve_inbound_receiver(config, protocol_root, runtime=sys.modules[__name__])


def _serve_oauth_callback(config: Mapping[str, Any], protocol_root: Path) -> None:
    _http.serve_oauth_callback(config, protocol_root, runtime=sys.modules[__name__])


def serve(role_name: str, *, configuration_path: Path, protocol_root: Path) -> None:
    _http.serve(
        role_name,
        configuration_path=configuration_path,
        protocol_root=protocol_root,
        runtime=sys.modules[__name__],
    )


def _service_access(
    role_name: str, role: ServiceRole
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    return _admin.service_access(role_name, role)


def health() -> None:
    _http.health(runtime=sys.modules[__name__])


def administrative_status(
    config: Mapping[str, Any],
    *,
    artifact_lock_path: Path = Path("/opt/jarvis/deployment/artifacts.lock.json"),
) -> dict[str, object]:
    return _admin.administrative_status(
        config, artifact_lock_path=artifact_lock_path, runtime=sys.modules[__name__]
    )


def _parser() -> argparse.ArgumentParser:
    return _cli.parser(runtime=sys.modules[__name__])


def main(argv: Sequence[str] | None = None) -> int:
    return _cli.main(argv, runtime=sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
