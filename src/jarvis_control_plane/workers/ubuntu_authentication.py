"""Ubuntu local Unix-channel authentication and readiness evidence."""

from __future__ import annotations

import os
import socket
import stat
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Protocol

from ..ports import ActionDispatcherError


class UbuntuWorkerReadiness(str, Enum):
    """The native worker states relevant at the dispatch barrier."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class UbuntuLocalPeerExpectation:
    """Exact OS identity and socket boundary trusted for one local peer."""

    peer_uid: int
    socket_owner_uid: int
    socket_path: str
    socket_mode: int = 0o600

    def __post_init__(self) -> None:
        for name in ("peer_uid", "socket_owner_uid"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"expected Ubuntu {name} is invalid")
        if not PurePosixPath(self.socket_path).is_absolute():
            raise ValueError("expected Ubuntu worker socket path must be absolute")
        if str(PurePosixPath(self.socket_path)) != self.socket_path:
            raise ValueError("expected Ubuntu worker socket path must be canonical")
        if self.socket_mode != 0o600:
            raise ValueError("expected Ubuntu worker socket mode must be 0600")

    def matches(self, peer: UbuntuLocalPeerIdentity) -> bool:
        return (
            peer.peer_uid == self.peer_uid
            and peer.socket_owner_uid == self.socket_owner_uid
            and peer.socket_path == self.socket_path
            and peer.socket_mode == self.socket_mode
        )


@dataclass(frozen=True, slots=True)
class UbuntuLocalPeerIdentity:
    """Identity evidence obtained from one connected local Unix socket."""

    peer_pid: int
    peer_uid: int
    peer_gid: int
    socket_path: str
    socket_owner_uid: int
    socket_mode: int
    connection_id: str

    def __post_init__(self) -> None:
        for name in ("peer_pid", "peer_uid", "peer_gid", "socket_owner_uid"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Ubuntu local {name} must be a non-negative integer")
        if self.peer_pid < 1:
            raise ValueError("Ubuntu local peer PID must be positive")
        if self.socket_mode != 0o600:
            raise ValueError("Ubuntu worker socket mode must be exactly 0600")
        if not self.socket_path.startswith("/"):
            raise ValueError("Ubuntu worker socket path must be absolute")
        if not self.connection_id.strip():
            raise ValueError("Ubuntu local connection identifier must be non-blank")


class UbuntuLocalAuthenticator(Protocol):
    """OS-backed authentication seam for the already-connected local channel."""

    def authenticate(self, *, timeout_seconds: int) -> UbuntuLocalPeerIdentity: ...

    def binds(self, connection: socket.socket) -> bool: ...


class ControlledUbuntuLocalAuthenticator:
    """Deterministic local-channel identity provider for contract tests."""

    def __init__(
        self,
        identity: UbuntuLocalPeerIdentity,
        *,
        connection: socket.socket | None = None,
    ) -> None:
        self.identity = identity
        self._connection = connection

    def authenticate(self, *, timeout_seconds: int) -> UbuntuLocalPeerIdentity:
        if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 30:
            raise ValueError("Ubuntu local authentication timeout is invalid")
        return self.identity

    def binds(self, connection: socket.socket) -> bool:
        return self._connection is connection


class UnixSocketUbuntuLocalAuthenticator:
    """Read Linux ``SO_PEERCRED`` evidence from one accepted Unix connection.

    This class neither creates nor exposes a listener.  The native service's
    manually activated socket owner passes its already-accepted connection in;
    authentication then binds that connection to OS credentials and the exact
    restricted socket inode.
    """

    def __init__(
        self,
        *,
        connection: socket.socket,
        socket_path: str,
        connection_id: str,
    ) -> None:
        unix_family = getattr(socket, "AF_UNIX", None)
        if unix_family is None or connection.family != unix_family:
            raise ValueError("Ubuntu local channel must be a Unix socket")
        if not os.path.isabs(socket_path):
            raise ValueError("Ubuntu worker socket path must be absolute")
        if os.path.realpath(socket_path) != socket_path:
            raise ValueError("Ubuntu worker socket path must be canonical")
        if not connection_id.strip():
            raise ValueError("Ubuntu local connection identifier must be non-blank")
        self._connection = connection
        self._socket_path = socket_path
        self._connection_id = connection_id.strip()

    def binds(self, connection: socket.socket) -> bool:
        return self._connection is connection

    def authenticate(self, *, timeout_seconds: int) -> UbuntuLocalPeerIdentity:
        if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 30:
            raise ValueError("Ubuntu local authentication timeout is invalid")
        peer_credentials_option = getattr(socket, "SO_PEERCRED", None)
        if not isinstance(peer_credentials_option, int):
            raise ActionDispatcherError("Linux peer credentials are unavailable")
        try:
            socket_info = os.lstat(self._socket_path)
            if not stat.S_ISSOCK(socket_info.st_mode):
                raise ActionDispatcherError("Ubuntu local channel is not a socket")
            mode = stat.S_IMODE(socket_info.st_mode)
            if mode != 0o600:
                raise ActionDispatcherError(
                    "Ubuntu worker socket permissions are not restricted"
                )
            channel_paths = (
                self._connection.getsockname(),
                self._connection.getpeername(),
            )
            if not any(
                isinstance(channel_path, str)
                and channel_path
                and os.path.samefile(channel_path, self._socket_path)
                for channel_path in channel_paths
            ):
                raise ActionDispatcherError("Ubuntu worker socket identity changed")
            raw = self._connection.getsockopt(
                socket.SOL_SOCKET, peer_credentials_option, struct.calcsize("3i")
            )
            peer_pid, peer_uid, peer_gid = struct.unpack("3i", raw)
        except ActionDispatcherError:
            raise
        except (OSError, ValueError, struct.error) as exc:
            raise ActionDispatcherError(
                "Ubuntu local peer authentication failed"
            ) from exc
        return UbuntuLocalPeerIdentity(
            peer_pid=peer_pid,
            peer_uid=peer_uid,
            peer_gid=peer_gid,
            socket_path=self._socket_path,
            socket_owner_uid=socket_info.st_uid,
            socket_mode=mode,
            connection_id=self._connection_id,
        )
