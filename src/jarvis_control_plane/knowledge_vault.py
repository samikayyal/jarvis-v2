"""Compatibility facade for the bounded knowledge-vault read integration."""

from __future__ import annotations

# These imports intentionally retain the former module's public and private
# names. Existing composition roots and contract tests import this module
# directly, while implementation ownership lives under integrations.vault.
# ruff: noqa: F401
import os
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from subprocess import DEVNULL, CompletedProcess, TimeoutExpired, run
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .integrations.vault.common import (
    _EXCLUDED_TOP_LEVEL_DIRECTORIES,
    _remaining_seconds,
)
from .integrations.vault.errors import (
    VaultPushPreDispatchFailure,
    VaultPushUnknownOutcome,
    VaultReadError,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
    VaultSynchronizationError,
)
from .integrations.vault.read_connector import KnowledgeVaultConnector
from .integrations.vault.read_models import (
    _MAX_COMPLETE_NOTE_CHARS,
    _MAX_QUERY_CHARS,
    _MAX_RETURNED_EXCERPTS,
    KnowledgeVaultReadResult,
    VaultExcerpt,
    VaultReadInput,
    VaultSynchronizationMetadataStore,
    VaultSynchronizer,
)
from .integrations.vault.read_policy import (
    _MARKDOWN_LINK,
    _MAX_BYTES_PER_NOTE,
    _MAX_EXCERPT_CHARS,
    _MAX_NOTES_INSPECTED,
    _MAX_TOTAL_BYTES_SCANNED,
    _WIKILINK,
    _note_title,
    _stale_warning,
    _unique_paths,
    _VaultReadBudget,
)
from .integrations.vault.synchronizer import (
    ControlledVaultSynchronizer,
    SubprocessVaultSynchronizer,
)
from .integrations.vault.write_policy import (
    _UNIFIED_HUNK,
    _normalise_staged_diff,
)

__all__ = [
    "ControlledVaultSynchronizer",
    "KnowledgeVaultConnector",
    "KnowledgeVaultReadResult",
    "SubprocessVaultSynchronizer",
    "VaultExcerpt",
    "VaultPushPreDispatchFailure",
    "VaultPushUnknownOutcome",
    "VaultReadError",
    "VaultReadInput",
    "VaultRemoteUnavailable",
    "VaultRepositoryConflict",
    "VaultSynchronizationError",
    "VaultSynchronizationMetadataStore",
    "VaultSynchronizer",
]
