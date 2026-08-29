"""Pure working-session state and lifecycle transitions for Jarvis V1.

This module deliberately models a Jarvis *working session*, not the named
logical connection owned by the WhatsApp messaging gateway.  It contains no
receiver, broker, persistence, connector, or model integration.  Callers pass
state in and receive a new immutable state plus a bounded transition record.

The later control-plane integration can persist the returned state and turn
the effects into audit, cancellation, and dispatch operations.  Until then,
the generation-bound :class:`CancellationToken` is the pure boundary that
prevents a result from an old request from being applied to a newer state.
"""

# ruff: noqa: F401, I001 -- this module intentionally re-exports its historical API.

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from .domain.session_actions import (
    ActionDispatchRecord,
    DispatchStatus,
    PendingActionState,
    ProposalPresentationFragment,
    _pending_action_digest,
)
from .domain.session_core import (
    _MAX_LEGACY_PERMISSION_MIGRATION_COUNT,
    _WORKING_SESSION_SCHEMA_VERSION,
    ALLOWED_SESSION_MINUTES,
    CANONICAL_MODELS,
    CANONICAL_REASONING_LEVELS,
    DEFAULT_SESSION_MINUTES,
    MODELS,
    PENDING_ACTION_MINUTES,
    PENDING_ACTION_TTL,
    REASONING_LEVELS,
    SESSION_MINUTES,
    Clock,
    DurableStateReferences,
    HistoryEntry,
    InvariantViolation,
    ModelAvailability,
    PendingActionPort,
    PendingActionStore,
    PermissionLifetime,
    PermissionPort,
    PermissionStore,
    ProposalPresentationStatus,
    ReadinessLevel,
    ReadinessPort,
    ReadinessProvider,
    ReadinessState,
    RequestPhase,
    ServiceReadiness,
    SessionConfig,
    SessionLifecycle,
    SessionStoreError,
    TransitionKind,
    _canonical_choice,
    _identifier,
    _now,
    _readiness_level,
    ensure_utc,
)
from .domain.session_requests import (
    ActiveRequestState,
    CancellationToken,
    CommandPermissionComponent,
    CommandPermissionIdentity,
    CommandPermissionState,
    RequestResult,
)
from .domain.session_state import SessionTransition, WorkingSession
from .models import AuditEvidence


from .application.sessions.lifecycle import (
    approve_pending_action,
    complete_action_dispatch,
    expire_inactive_session,
    expire_pending_action,
    is_session_inactive,
    mark_action_dispatch_attempted,
    new_working_session,
    pending_action_is_expired,
    reconcile_action_cancellation,
    reject_pending_action,
    session_inactivity_suspended,
)
from .application.sessions.recovery import (
    apply_request_result,
    cancellation_token_is_current,
    interrupt_for_restart,
)
from .application.sessions.request import (
    _busy_or_pending,
    _transition,
    accept_request,
    cancel_active_request,
    create_working_session,
    install_pending_action,
    mark_proposal_presented,
    record_proposal_fragment,
)


from .application.sessions.views import (
    StatusPendingActionView,
    StatusPermissionView,
    StatusReadinessView,
    StatusRequestView,
    StatusView,
    active_command_permissions,
    revoke_command_permission,
    revoke_command_permissions,
    status_view,
)
from .application.sessions.serialization import (
    _parse_timestamp,
    _session_from_json,
    _session_json,
)
from .application.sessions.stores import (
    InMemoryWorkingSessionStore,
    SQLiteWorkingSessionStore,
    WorkingSessionStore,
    _locked_working_session_store,
)

SQLiteSessionStore = SQLiteWorkingSessionStore
SessionState = WorkingSession
WorkingSessionState = WorkingSession
WorkingSessionConfig = SessionConfig
DurableReferences = DurableStateReferences
PendingAction = PendingActionState
CommandPermission = CommandPermissionState
Permission = CommandPermissionState
Readiness = ReadinessState
ReadinessSnapshot = ReadinessState
RequestState = ActiveRequestState
Transition = SessionTransition
