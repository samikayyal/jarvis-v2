"""Focused worker modules behind the legacy control-plane facades.

The package is intentionally explicit: contracts and gateway policy are kept
separate from host-specific authentication, process/job execution, transports,
and runtime adapters. Runtime and socket-session modules load lazily so the
legacy gateway facade can initialize without a service-protocol cycle.
"""

from importlib import import_module

from .contracts import (
    WorkerExecutionError,
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    WorkerProgressSink,
    WorkerTransport,
)
from .controlled_transport import ControlledWorkerTransport
from .gateway import WorkerGateway
from .ipc_server import serve_ubuntu_worker_connection
from .ipc_transport import (
    ReconnectingUnixSocketUbuntuWorkerTransport,
    UnixSocketUbuntuWorkerTransport,
)
from .ubuntu_authentication import (
    ControlledUbuntuLocalAuthenticator,
    UbuntuLocalAuthenticator,
    UbuntuLocalPeerExpectation,
    UbuntuLocalPeerIdentity,
    UbuntuWorkerReadiness,
    UnixSocketUbuntuLocalAuthenticator,
)
from .ubuntu_process_scope import (
    ControlledUbuntuProcessScope,
    SystemdUbuntuProcessScope,
    UbuntuProcessScope,
)
from .ubuntu_service import UbuntuWorkerService
from .ubuntu_worker_runner import main as ubuntu_worker_runner_main
from .windows_job import (
    SubprocessWindowsJobObjectExecutor,
    WindowsJobObjectExecutor,
    WindowsJobObjectWorkerSession,
    WindowsWorkerSession,
)
from .windows_mtls import (
    WindowsMtlsClientConfig,
    WindowsWorkerRegistration,
    WindowsWorkerSessionEvidence,
    authenticate_windows_worker_session,
    open_windows_worker_mtls_session,
)
from .windows_transport import (
    ControlledOutboundWindowsWorkerTransport,
    ControlledWindowsWorkerSession,
    OutboundWindowsWorkerTransport,
)

_DEFERRED_MODULES = {
    "SocketWindowsWorkerSession": ".windows_session",
    "WindowsMtlsServerConfig": ".windows_session",
    "WindowsWorkerMtlsAcceptor": ".windows_session",
    "run_windows_worker_client": ".windows_session",
    "serve_windows_worker_session": ".windows_session",
    "UbuntuWorkerRuntimeConfig": ".runtime",
    "WindowsWorkerRuntimeConfig": ".runtime",
    "load_ubuntu_config": ".runtime",
    "load_windows_config": ".runtime",
    "run_ubuntu_worker": ".runtime",
    "run_windows_service": ".runtime",
    "run_windows_worker_loop": ".runtime",
}


def __getattr__(name: str) -> object:
    module_name = _DEFERRED_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "ControlledOutboundWindowsWorkerTransport",
    "ControlledUbuntuLocalAuthenticator",
    "ControlledUbuntuProcessScope",
    "ControlledWindowsWorkerSession",
    "ControlledWorkerTransport",
    "OutboundWindowsWorkerTransport",
    "ReconnectingUnixSocketUbuntuWorkerTransport",
    "SocketWindowsWorkerSession",
    "SubprocessWindowsJobObjectExecutor",
    "SystemdUbuntuProcessScope",
    "UbuntuLocalAuthenticator",
    "UbuntuLocalPeerExpectation",
    "UbuntuLocalPeerIdentity",
    "UbuntuProcessScope",
    "UbuntuWorkerReadiness",
    "UbuntuWorkerRuntimeConfig",
    "UbuntuWorkerService",
    "UnixSocketUbuntuLocalAuthenticator",
    "UnixSocketUbuntuWorkerTransport",
    "WindowsJobObjectExecutor",
    "WindowsJobObjectWorkerSession",
    "WindowsMtlsClientConfig",
    "WindowsMtlsServerConfig",
    "WindowsWorkerMtlsAcceptor",
    "WindowsWorkerRegistration",
    "WindowsWorkerRuntimeConfig",
    "WindowsWorkerSession",
    "WindowsWorkerSessionEvidence",
    "WorkerExecutionError",
    "WorkerExecutionLimits",
    "WorkerExecutionResult",
    "WorkerExecutionStatus",
    "WorkerGateway",
    "WorkerIdentity",
    "WorkerInvocation",
    "WorkerOutputStream",
    "WorkerProgressEvent",
    "WorkerProgressKind",
    "WorkerProgressSink",
    "WorkerTransport",
    "authenticate_windows_worker_session",
    "load_ubuntu_config",
    "load_windows_config",
    "open_windows_worker_mtls_session",
    "run_ubuntu_worker",
    "run_windows_service",
    "run_windows_worker_client",
    "run_windows_worker_loop",
    "serve_ubuntu_worker_connection",
    "serve_windows_worker_session",
    "ubuntu_worker_runner_main",
]
