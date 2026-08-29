"""Windows mTLS configuration and dual-identity admission."""

from __future__ import annotations

import json
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path

from ..ports import ActionDispatcherError
from .contracts import WorkerIdentity

_AUTHENTICATED_EVIDENCE_TOKEN = object()
_APPLICATION_HELLO_LIMIT_BYTES = 4096


@dataclass(frozen=True, slots=True)
class WindowsMtlsClientConfig:
    """Files and private-overlay endpoint for the outbound worker connection."""

    overlay_host: str
    overlay_port: int
    server_name: str
    ca_file: Path
    certificate_file: Path
    private_key_file: Path

    def __post_init__(self) -> None:
        for name in ("overlay_host", "server_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Windows mTLS {name.replace('_', ' ')} is required")
            if value != value.strip():
                raise ValueError(
                    f"Windows mTLS {name.replace('_', ' ')} must be canonical"
                )
        if (
            isinstance(self.overlay_port, bool)
            or not isinstance(self.overlay_port, int)
            or not 1 <= self.overlay_port <= 65535
        ):
            raise ValueError("Windows mTLS overlay port is invalid")
        for name in ("ca_file", "certificate_file", "private_key_file"):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise ValueError(
                    f"Windows mTLS {name.replace('_', ' ')} must be absolute"
                )
            object.__setattr__(self, name, value)


def open_windows_worker_mtls_session(
    config: WindowsMtlsClientConfig, *, timeout_seconds: int = 10
) -> ssl.SSLSocket:
    """Initiate the worker's outbound TLS 1.3 connection.

    This helper is deliberately not called during import or application start.
    Service activation, credential provisioning, overlay routing, and retry
    supervision remain manual deployment concerns.
    """

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 30
    ):
        raise ValueError("Windows mTLS timeout must be between one and 30 seconds")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=str(config.ca_file))
    context.load_cert_chain(
        certfile=str(config.certificate_file), keyfile=str(config.private_key_file)
    )
    raw = socket.create_connection(
        (config.overlay_host, config.overlay_port), timeout=timeout_seconds
    )
    try:
        tls = context.wrap_socket(raw, server_hostname=config.server_name)
        tls.settimeout(None)
        return tls
    except BaseException:
        raw.close()
        raise


@dataclass(frozen=True, slots=True)
class WindowsWorkerRegistration:
    """Administrative identity registered for the one Windows worker."""

    identity: WorkerIdentity
    certificate_identity: str
    application_identity: str
    heartbeat_interval_seconds: int = 10

    def __post_init__(self) -> None:
        if self.identity.host != "windows":
            raise ValueError("Windows worker registration must use the windows host")
        for name in ("certificate_identity", "application_identity"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Windows worker {name.replace('_', ' ')} is required")
            if value != value.strip():
                raise ValueError(
                    f"Windows worker {name.replace('_', ' ')} must be canonical"
                )
        if (
            isinstance(self.heartbeat_interval_seconds, bool)
            or not isinstance(self.heartbeat_interval_seconds, int)
            or not 1 <= self.heartbeat_interval_seconds <= 15
        ):
            raise ValueError(
                "Windows worker heartbeat interval must be between one and 15 seconds"
            )


@dataclass(frozen=True, slots=True, init=False)
class WindowsWorkerSessionEvidence:
    """Identity evidence from one authenticated outbound mTLS session.

    ``certificate_identity`` is populated only from the authenticated peer
    certificate. ``application_identity`` and the remaining fields come from
    the worker's closed application handshake.  Keeping the sources explicit
    prevents either identity from substituting for the other.
    """

    host: str
    worker_id: str
    connection_id: str
    certificate_identity: str
    application_identity: str
    heartbeat_interval_seconds: int
    _authenticated: bool

    def __init__(
        self,
        *,
        host: str,
        worker_id: str,
        connection_id: str,
        certificate_identity: str,
        application_identity: str,
        heartbeat_interval_seconds: int = 10,
        _token: object | None = None,
    ) -> None:
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "worker_id", worker_id)
        object.__setattr__(self, "connection_id", connection_id)
        object.__setattr__(self, "certificate_identity", certificate_identity)
        object.__setattr__(self, "application_identity", application_identity)
        object.__setattr__(
            self, "heartbeat_interval_seconds", heartbeat_interval_seconds
        )
        object.__setattr__(
            self, "_authenticated", _token is _AUTHENTICATED_EVIDENCE_TOKEN
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        for name in (
            "host",
            "worker_id",
            "connection_id",
            "certificate_identity",
            "application_identity",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Windows session {name.replace('_', ' ')} is required"
                )
            if value != value.strip():
                raise ValueError(
                    f"Windows session {name.replace('_', ' ')} must be canonical"
                )
        if (
            isinstance(self.heartbeat_interval_seconds, bool)
            or not isinstance(self.heartbeat_interval_seconds, int)
            or not 1 <= self.heartbeat_interval_seconds <= 15
        ):
            raise ValueError(
                "Windows session heartbeat interval must be between one and 15 seconds"
            )

    @property
    def worker_identity(self) -> WorkerIdentity:
        return WorkerIdentity(
            host=self.host,
            worker_id=self.worker_id,
            connection_id=self.connection_id,
        )

    @property
    def authenticated(self) -> bool:
        """Whether evidence came from the mTLS and closed-handshake factory."""

        return self._authenticated


def authenticate_windows_worker_session(
    *,
    registration: WindowsWorkerRegistration,
    tls_socket: ssl.SSLSocket,
    application_hello: bytes,
) -> WindowsWorkerSessionEvidence:
    """Bind TLS peer-certificate evidence to one closed application hello."""

    if tls_socket.version() != "TLSv1.3":
        raise ActionDispatcherError("Windows worker session did not negotiate TLS 1.3")
    peer = tls_socket.getpeercert()
    if not isinstance(peer, dict):
        raise ActionDispatcherError("Windows worker peer certificate is unavailable")
    subject_alt_names = peer.get("subjectAltName", ())
    certificate_identities = {
        value
        for kind, value in subject_alt_names
        if kind == "URI" and isinstance(value, str)
    }
    if certificate_identities != {registration.certificate_identity}:
        raise ActionDispatcherError("Windows worker certificate identity mismatch")
    if not isinstance(application_hello, bytes) or not application_hello:
        raise ActionDispatcherError("Windows worker application hello is invalid")
    if len(application_hello) > _APPLICATION_HELLO_LIMIT_BYTES:
        raise ActionDispatcherError("Windows worker application hello is oversized")
    try:
        hello = json.loads(application_hello.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActionDispatcherError(
            "Windows worker application hello is malformed"
        ) from exc
    required = {
        "host",
        "worker_id",
        "connection_id",
        "application_identity",
        "heartbeat_interval_seconds",
    }
    if not isinstance(hello, dict) or set(hello) != required:
        raise ActionDispatcherError("Windows worker application hello schema mismatch")
    string_fields = required - {"heartbeat_interval_seconds"}
    heartbeat_interval = hello["heartbeat_interval_seconds"]
    if (
        any(not isinstance(hello[key], str) for key in string_fields)
        or isinstance(heartbeat_interval, bool)
        or not isinstance(heartbeat_interval, int)
        or not 1 <= heartbeat_interval <= 15
    ):
        raise ActionDispatcherError("Windows worker application hello schema mismatch")
    evidence = WindowsWorkerSessionEvidence(
        host=hello["host"],
        worker_id=hello["worker_id"],
        connection_id=hello["connection_id"],
        certificate_identity=registration.certificate_identity,
        application_identity=hello["application_identity"],
        heartbeat_interval_seconds=heartbeat_interval,
        _token=_AUTHENTICATED_EVIDENCE_TOKEN,
    )
    if (
        evidence.worker_identity != registration.identity
        or evidence.application_identity != registration.application_identity
        or evidence.heartbeat_interval_seconds
        != registration.heartbeat_interval_seconds
    ):
        raise ActionDispatcherError("Windows worker application identity mismatch")
    return evidence


__all__ = [
    "WindowsMtlsClientConfig",
    "WindowsWorkerRegistration",
    "WindowsWorkerSessionEvidence",
    "authenticate_windows_worker_session",
    "open_windows_worker_mtls_session",
]
