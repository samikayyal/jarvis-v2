"""Compatibility facade for the legacy ubuntu_worker module.

Ubuntu authentication, process scope, and worker-service implementations are
kept in separate modules so each seam remains small and independently tested.
"""

from __future__ import annotations

import queue
import socket
import subprocess
import sys

from .workers.contracts import (
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    WorkerProgressSink,
)
from .workers.ubuntu_authentication import (
    ControlledUbuntuLocalAuthenticator,
    UbuntuLocalAuthenticator,
    UbuntuLocalPeerExpectation,
    UbuntuLocalPeerIdentity,
    UbuntuWorkerReadiness,
    UnixSocketUbuntuLocalAuthenticator,
)
from .workers.ubuntu_process_execution import (
    _COMPOUND_METADATA_LIMIT_BYTES,
    _extract_compound_result,
    _render_captured,
)
from .workers.ubuntu_process_models import (
    _RunningSystemdScope,
    _StartingSystemdScope,
    _SystemdUbuntuProcessScopeAdapter,
    _UbuntuProcessScopeAdapter,
)
from .workers.ubuntu_process_scope import (
    ControlledUbuntuProcessScope,
    SystemdUbuntuProcessScope,
    UbuntuProcessScope,
)
from .workers.ubuntu_service import (
    UbuntuWorkerService,
    _ActionRecord,
    _ActionState,
    _bound_result,
    _truncate_utf8,
)
from .workers.ubuntu_worker_runner import COMPOUND_RESULT_MARKER

__all__ = [
    "COMPOUND_RESULT_MARKER",
    "_COMPOUND_METADATA_LIMIT_BYTES",
    "ControlledUbuntuLocalAuthenticator",
    "ControlledUbuntuProcessScope",
    "SystemdUbuntuProcessScope",
    "UbuntuLocalAuthenticator",
    "UbuntuLocalPeerExpectation",
    "UbuntuLocalPeerIdentity",
    "UbuntuProcessScope",
    "UbuntuWorkerReadiness",
    "UbuntuWorkerService",
    "UnixSocketUbuntuLocalAuthenticator",
    "WorkerExecutionLimits",
    "WorkerExecutionResult",
    "WorkerExecutionStatus",
    "WorkerIdentity",
    "WorkerInvocation",
    "WorkerOutputStream",
    "WorkerProgressEvent",
    "WorkerProgressKind",
    "WorkerProgressSink",
    "_ActionRecord",
    "_ActionState",
    "_RunningSystemdScope",
    "_StartingSystemdScope",
    "_SystemdUbuntuProcessScopeAdapter",
    "_UbuntuProcessScopeAdapter",
    "_bound_result",
    "_extract_compound_result",
    "_render_captured",
    "_truncate_utf8",
    "queue",
    "socket",
    "subprocess",
    "sys",
]
