"""Compatibility facade for the diagnostic trace implementation.

The diagnostic implementation is grouped by responsibility under
``jarvis_control_plane.diagnostics``.  This module intentionally keeps the
historic import surface, including private names used by tests and local
composition roots.
"""

# This facade deliberately re-exports private implementation names.  They are
# part of the established local test/composition seam even though they are not
# the normal control-plane API.
# ruff: noqa: F401

from __future__ import annotations

# These module imports remain available for legacy monkeypatch paths.  The
# extracted implementation imports the same module objects, so patching e.g.
# ``traces.shutil.disk_usage`` still affects filesystem capacity checks.
import base64
import json
import math
import shutil
import sqlite3
import tempfile
import threading
import traceback
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime
from enum import Enum
from multiprocessing import Pipe, Process
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, TypeVar

from .diagnostics.capacity import (
    _FileSystemTraceCapacityProvider,
    _StaticTraceCapacityProvider,
    _TraceCapacityProvider,
)
from .diagnostics.recorder import (
    DiagnosticTraceRecorder,
    _TraceAdmission,
    _TraceExecution,
    _TraceExecutionState,
    _TracePersistence,
)
from .diagnostics.records import (
    DEFAULT_TRACE_RESERVATION_BYTES,
    MAX_TRACE_RESERVATION_BYTES,
    DiagnosticTrace,
    DiagnosticTraceLimits,
    _validate_non_negative_int,
    _validate_positive_int,
)
from .diagnostics.sqlite_store import SQLiteDiagnosticTraceStore
from .diagnostics.store import (
    InMemoryDiagnosticTraceStore,
    _DiagnosticTraceStoreBase,
)
from .diagnostics.values import (
    TraceReservation,
    _canonical_json,
    _freeze_trace_value,
    _safe_repr,
    _safe_text,
    _thaw_trace_value,
    _trace_value,
    _TraceValueEncoder,
)
from .diagnostics.writer import (
    TraceWriterCapability,
    _build_trace_writer_store,
    _raise_trace_writer_error,
    _start_trace_writer_service,
    _trace_writer_error,
    _trace_writer_mailbox,
    _trace_writer_process_main,
    _TraceWriterLifecycle,
    _TraceWriterRuntime,
)
from .models import ensure_utc
from .ports import (
    Clock,
    DiagnosticTraceStore,
    IdGenerator,
    TraceCapacityError,
    TraceWriteError,
)
from .writer_capability import close_writer_capability
