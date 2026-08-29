"""Compatibility facade for the legacy windows_worker module."""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess

from .application.compatibility import install_mirrors
from .workers import windows_job as _windows_job
from .workers import windows_transport as _windows_transport
from .workers.windows_job import (
    SubprocessWindowsJobObjectExecutor,
    WindowsJobObjectExecutor,
    WindowsJobObjectWorkerSession,
    WindowsWorkerSession,
)

_RunningWindowsJob = _windows_job._RunningWindowsJob
_ActionRecord = _windows_transport._ActionRecord
_ActionState = _windows_transport._ActionState
from .workers.windows_mtls import (
    WindowsMtlsClientConfig,
    WindowsWorkerRegistration,
    WindowsWorkerSessionEvidence,
    authenticate_windows_worker_session,
    open_windows_worker_mtls_session,
)
from .workers.windows_transport import (
    ControlledOutboundWindowsWorkerTransport,
    ControlledWindowsWorkerSession,
    OutboundWindowsWorkerTransport,
)

_CREATE_SUSPENDED = 0x00000004

__all__ = [
    "ControlledOutboundWindowsWorkerTransport",
    "ControlledWindowsWorkerSession",
    "OutboundWindowsWorkerTransport",
    "SubprocessWindowsJobObjectExecutor",
    "WindowsJobObjectExecutor",
    "WindowsJobObjectWorkerSession",
    "WindowsMtlsClientConfig",
    "WindowsWorkerRegistration",
    "WindowsWorkerSession",
    "WindowsWorkerSessionEvidence",
    "authenticate_windows_worker_session",
    "json",
    "open_windows_worker_mtls_session",
    "os",
    "socket",
    "ssl",
    "subprocess",
]

install_mirrors(
    __name__,
    {
        "_ActionRecord": (_windows_transport,),
        "_ActionState": (_windows_transport,),
        "_RunningWindowsJob": (_windows_job,),
    },
)
