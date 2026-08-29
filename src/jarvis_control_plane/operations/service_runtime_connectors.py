"""Connector composition roots for Google, OpenWA, the vault, and workers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import RLock
from typing import Any

from ..models import FrozenActionProposal
from ..ports import ActionCancellationResult, ActionCancellationStatus


class GoogleActionDispatcher:
    """Route Google action lifecycle calls to the owner of the frozen action."""

    def __init__(
        self,
        *,
        gmail: Any,
        acceptance_failpoint: Any = None,
    ) -> None:
        self._gmail = gmail
        self._acceptance_failpoint = acceptance_failpoint
        self._owners: dict[str, object] = {}
        self._lock = RLock()

    def _owner(self, action: FrozenActionProposal) -> object:
        if action.kind in {"gmail_send", "gmail_reply"}:
            return self._gmail
        raise ValueError("action kind is outside the Google connector")

    def bind_proposal(self, action: FrozenActionProposal) -> FrozenActionProposal:
        owner = self._owner(action)
        binder = getattr(owner, "bind_proposal", None)
        bound = binder(action) if callable(binder) else action
        if self._acceptance_failpoint is not None:
            target = {
                "gmail_send": ("gmail", "gmail_send"),
                "gmail_reply": ("gmail", "gmail_reply"),
            }.get(bound.kind)
            if target is not None and target == (
                self._acceptance_failpoint.spec.service,
                self._acceptance_failpoint.spec.operation,
            ):
                bound_ok = self._acceptance_failpoint.bind_action(
                    service=target[0], operation=target[1], action_id=bound.action_id
                )
                if not bound_ok:
                    raise ValueError(
                        "reviewed acceptance failpoint could not bind the frozen "
                        "action durably"
                    )
        return bound

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

    def finalize(self, *, action_id: str) -> None:
        with self._lock:
            self._owners.pop(action_id, None)


def google_operations(
    config: Mapping[str, Any], *, runtime: Any
) -> Mapping[str, Callable[..., object]]:
    deployment = config.get("deployment")
    if not isinstance(deployment, Mapping):
        raise runtime.CompositionError("Google deployment configuration is unavailable")
    identity = runtime._require_text(deployment.get("google_subject"), "google_subject")
    credentials = runtime._credential_json(
        runtime.Path("/run/credentials/google/credentials.json")
    )
    client_id = runtime._require_text(credentials.get("client_id"), "Google client_id")
    client_secret = runtime._require_text(
        credentials.get("client_secret"), "Google client_secret"
    )
    credential_store = runtime.FileGoogleCredentialStore("/run/credentials/google")
    state_store = runtime.SQLiteGoogleOAuthStateStore(
        "/run/credentials/google/oauth-state.sqlite3"
    )
    connection = state_store.get_connection()
    if not connection.connected and credential_store.current is not None:
        try:
            credential_store.delete()
        except runtime.GoogleOAuthError as exc:
            raise runtime.CompositionError(
                "disconnected Google credential could not be discarded"
            ) from exc
    google_trace_root = runtime.Path("/var/lib/jarvis/google-traces")
    clock, ids, trace = runtime._service_trace(config, "google", root=google_trace_root)
    audit = runtime.RemoteAuditBoundary(
        runtime._client(
            config, client_identity="jarvis-google", server_role="audit_service"
        )
    )
    provider = runtime.GoogleLiveOAuthProvider(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=runtime._require_text(
            deployment.get("oauth_callback_url"), "oauth_callback_url"
        ),
    )
    lifecycle = runtime.GoogleOAuthLifecycle(
        configured_identity=identity,
        state_store=state_store,
        credential_store=credential_store,
        provider=provider,
        audit=audit,
        trace=trace,
        clock=clock,
        ids=ids,
    )
    acceptance_failpoint = runtime._reviewed_acceptance_failpoint(
        config, durable_root=google_trace_root / "acceptance-failpoints"
    )
    reads = runtime.GoogleReadConnector(
        configured_identity=identity,
        credential_store=credential_store,
        provider=runtime.GoogleApiReadProvider(
            client_id=client_id, client_secret=client_secret
        ),
        audit=audit,
        trace=trace,
        clock=clock,
        ids=ids,
        connection_binding=lifecycle.connection_binding,
        on_invalid_grant=lambda generation: lifecycle.handle_refresh_failure(
            "invalid_grant", connection_generation=generation
        ),
    )
    gmail = runtime.GmailWriteConnector(
        configured_identity=identity,
        credential_store=credential_store,
        provider=runtime.GmailApiWriteProvider(
            client_id=client_id, client_secret=client_secret
        ),
        audit=audit,
        trace=trace,
        clock=clock,
        ids=ids,
        connection_binding=lifecycle.connection_binding,
        on_invalid_grant=lambda: lifecycle.handle_refresh_failure("invalid_grant"),
        acceptance_failpoint=(
            acceptance_failpoint
            if acceptance_failpoint is not None
            and acceptance_failpoint.spec.service == "gmail"
            else None
        ),
    )
    operations = {
        name: getattr(reads, name)
        for name in runtime.SERVICE_ROLES["google_connector"].operations
        if name in {"current", "current_connection_generation"}
        or name.startswith(("gmail_", "drive_"))
    }
    operations.update(
        runtime.OwnedActionService(
            runtime._GoogleActionDispatcher(
                gmail=gmail,
                acceptance_failpoint=acceptance_failpoint,
            )  # type: ignore[arg-type]
        ).operations()
    )

    def start_authorization(
        *, operation_id: str, requested_scopes: tuple[str, ...]
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


def vault_operations(
    config: Mapping[str, Any], *, runtime: Any
) -> Mapping[str, Callable[..., object]]:
    deployment = config.get("deployment")
    if not isinstance(deployment, Mapping):
        raise runtime.CompositionError("vault deployment configuration is unavailable")
    root = runtime.Path("/var/lib/jarvis/vault")
    if not root.is_dir():
        raise runtime.CompositionError("knowledge-vault clone is unavailable")
    repository = runtime.SubprocessVaultRepository(
        ssh_executable=runtime.Path("/usr/bin/ssh"),
        ssh_config_path=runtime.Path("/run/credentials/vault/ssh_config"),
        known_hosts_path=runtime.Path("/run/credentials/vault/known_hosts"),
        proxy_command=(
            "/usr/local/bin/python",
            "/opt/jarvis/src/jarvis_control_plane/egress_proxy.py",
            "%h",
            "%p",
            "--proxy-host",
            "vault_egress_proxy",
        ),
    )
    repository.validate_remote(
        root,
        runtime._require_text(deployment.get("vault_remote"), "vault_remote"),
    )
    clock = runtime.SystemClock()
    reads = runtime.KnowledgeVaultConnector(
        root=root, synchronizer=repository, now=clock.now
    )
    note_directories = deployment.get("vault_note_directories")
    if not isinstance(note_directories, list) or not all(
        isinstance(item, str) for item in note_directories
    ):
        raise runtime.CompositionError("vault note directories are invalid")
    writes = runtime.KnowledgeVaultWriteConnector(
        root=root,
        repository=repository,
        now=clock.now,
        allowed_note_directories=tuple(note_directories),
        timeout_seconds=runtime._vault_write_timeout(config),
    )
    operations: dict[str, Callable[..., object]] = {
        "read": lambda payload, deadline=None: reads.read(
            runtime.VaultReadInput.model_validate(payload), deadline=deadline
        ).model_dump(mode="json"),
        "propose": writes.propose,
    }
    operations.update(runtime.OwnedActionService(writes).operations())
    return operations


def worker_operations(
    config: Mapping[str, Any], *, runtime: Any
) -> Mapping[str, Callable[..., object]]:
    deployment = config.get("deployment")
    paths = config.get("paths")
    if not isinstance(deployment, Mapping) or not isinstance(paths, Mapping):
        raise runtime.CompositionError("worker deployment configuration is unavailable")
    credentials = runtime._credential_json(
        runtime.Path("/run/credentials/windows-worker/credentials.json")
    )
    socket_path = runtime._require_text(
        paths.get("ubuntu_worker_socket"), "ubuntu_worker_socket"
    )
    ubuntu_identity = runtime.WorkerIdentity(
        host="ubuntu",
        worker_id=runtime._require_text(
            deployment.get("ubuntu_worker_identity"), "ubuntu_worker_identity"
        ),
        connection_id=runtime._require_text(
            credentials.get("ubuntu_connection_id"), "ubuntu_connection_id"
        ),
    )
    ubuntu_peer = runtime.UbuntuLocalPeerExpectation(
        peer_uid=int(credentials.get("ubuntu_peer_uid")),
        socket_owner_uid=int(credentials.get("ubuntu_socket_owner_uid")),
        socket_path=socket_path,
    )

    def connect_ubuntu() -> Any:
        connection = runtime.socket.socket(
            runtime.socket.AF_UNIX, runtime.socket.SOCK_STREAM
        )
        try:
            connection.connect(socket_path)
            return runtime.UnixSocketUbuntuWorkerTransport(
                connection=connection,
                authenticator=runtime.UnixSocketUbuntuLocalAuthenticator(
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
        raise runtime.CompositionError(
            "native Ubuntu worker socket is unavailable"
        ) from exc
    ubuntu = runtime.ReconnectingUnixSocketUbuntuWorkerTransport(
        connect=connect_ubuntu,
        initial=initial_ubuntu,
    )
    windows_identity = runtime.WorkerIdentity(
        host="windows",
        worker_id=runtime._require_text(
            deployment.get("windows_worker_identity"), "windows_worker_identity"
        ),
        connection_id=runtime._require_text(
            credentials.get("windows_connection_id"), "windows_connection_id"
        ),
    )
    registration = runtime.WindowsWorkerRegistration(
        identity=windows_identity,
        certificate_identity=runtime._require_text(
            credentials.get("windows_certificate_identity"),
            "windows_certificate_identity",
        ),
        application_identity=runtime._require_text(
            credentials.get("windows_application_identity"),
            "windows_application_identity",
        ),
    )
    windows = runtime.OutboundWindowsWorkerTransport(registration=registration)
    acceptor = runtime.WindowsWorkerMtlsAcceptor(
        config=runtime.WindowsMtlsServerConfig(
            bind_host=runtime._require_text(
                credentials.get("windows_overlay_bind_host"),
                "windows_overlay_bind_host",
            ),
            bind_port=runtime._reviewed_windows_overlay_port(
                credentials.get("windows_overlay_bind_port")
            ),
            ca_file=runtime.Path("/run/credentials/windows-worker/worker-ca.pem"),
            certificate_file=runtime.Path(
                "/run/credentials/windows-worker/gateway-certificate.pem"
            ),
            private_key_file=runtime._private_key_path(
                runtime.Path("/run/credentials/windows-worker/gateway-private-key.pem")
            ),
        ),
        registration=registration,
        transport=windows,
    )
    acceptor.start()
    gateway = runtime.WorkerGateway(
        workers={"ubuntu": ubuntu, "windows": windows},
        registered_identities={
            "ubuntu": ubuntu_identity,
            "windows": windows_identity,
        },
    )
    operations = dict(runtime.OwnedActionService(gateway).operations())
    operations["current"] = gateway.current
    return operations
