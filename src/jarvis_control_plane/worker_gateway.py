"""Compatibility facade for the legacy worker_gateway module.

The public import path remains stable while the implementation lives under
the focused workers package.
"""

from __future__ import annotations

from .models import FrozenActionProposal
from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ActionDispatchHandle,
    WorkerReadiness,
)
from .terminal_policy import (
    TerminalAction,
    TerminalComponent,
    terminal_action_from_proposal,
)
from .workers.contracts import (
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
    _result_message,
)
from .workers.controlled_transport import (
    ControlledWorkerTransport,
    _RetainedCancellation,
    _WorkerTransportActionRecord,
    _WorkerTransportActionState,
)
from .workers.dispatch import _WorkerDispatchHandle
from .workers.gateway import WorkerGateway
from .workers.gateway_validation import (
    _bounded_result,
    _terminal_action,
    _truncate_output,
)

# Keep the serialized service-protocol names stable for callers that still
# import these contracts from the legacy module.
for _compatibility_type in (
    WorkerIdentity,
    WorkerExecutionStatus,
    WorkerExecutionLimits,
    WorkerInvocation,
    WorkerProgressKind,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerExecutionResult,
    _RetainedCancellation,
    _WorkerTransportActionRecord,
    _WorkerTransportActionState,
):
    _compatibility_type.__module__ = __name__
del _compatibility_type


__all__ = [
    "ActionCancellationResult",
    "ActionCancellationStatus",
    "ActionDispatchHandle",
    "ActionDispatcherError",
    "ControlledWorkerTransport",
    "FrozenActionProposal",
    "TerminalAction",
    "TerminalComponent",
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
    "WorkerReadiness",
    "WorkerTransport",
    "_RetainedCancellation",
    "_WorkerDispatchHandle",
    "_WorkerTransportActionRecord",
    "_WorkerTransportActionState",
    "_bounded_result",
    "_result_message",
    "_terminal_action",
    "_truncate_output",
    "terminal_action_from_proposal",
]
