"""Compatibility facade for the legacy windows_worker_session module."""

from __future__ import annotations

from .application.compatibility import install_mirrors
from .workers import windows_session as _windows_session
from .workers.windows_job import WindowsJobObjectExecutor
from .workers.windows_mtls import (
    WindowsMtlsClientConfig,
    WindowsWorkerRegistration,
    WindowsWorkerSessionEvidence,
    authenticate_windows_worker_session,
    open_windows_worker_mtls_session,
)
from .workers.windows_session import (
    SocketWindowsWorkerSession,
    WindowsMtlsServerConfig,
    WindowsWorkerMtlsAcceptor,
    run_windows_worker_client,
    serve_windows_worker_session,
)
from .workers.windows_transport import OutboundWindowsWorkerTransport

_receive_frame = _windows_session._receive_frame
_send_frame = _windows_session._send_frame

__all__ = [
    "OutboundWindowsWorkerTransport",
    "SocketWindowsWorkerSession",
    "WindowsJobObjectExecutor",
    "WindowsMtlsClientConfig",
    "WindowsMtlsServerConfig",
    "WindowsWorkerMtlsAcceptor",
    "WindowsWorkerRegistration",
    "WindowsWorkerSessionEvidence",
    "authenticate_windows_worker_session",
    "open_windows_worker_mtls_session",
    "run_windows_worker_client",
    "serve_windows_worker_session",
]

install_mirrors(
    __name__,
    {
        "_receive_frame": (_windows_session,),
        "_send_frame": (_windows_session,),
    },
)
