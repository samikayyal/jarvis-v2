"""Compatibility facade for the approval-gated knowledge-vault write edge."""

from __future__ import annotations

# Preserve the former module's import and monkeypatch surface. Implementation
# is grouped by proposal models, exact dispatch, rendering policy, and Git
# repository ownership under integrations.vault.
# ruff: noqa: F401
import difflib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import RLock
from time import monotonic
from typing import Literal, NoReturn, Protocol

from .integrations.vault.common import (
    _EXCLUDED_TOP_LEVEL_DIRECTORIES,
    _remaining_seconds,
)
from .integrations.vault.errors import (
    VaultPushPreDispatchFailure,
    VaultPushUnknownOutcome,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
    VaultWriteConflict,
    VaultWriteError,
    VaultWritePushPreDispatchFailure,
    VaultWriteRemoteUnavailable,
    VaultWriteRepositoryError,
)
from .integrations.vault.repository import ControlledVaultWriteRepository
from .integrations.vault.write_connector import (
    KnowledgeVaultWriteConnector,
    VaultWriteConnector,
)
from .integrations.vault.write_dispatch import (
    _VaultWriteDispatch,
    _VaultWriteProgress,
    _WriteExecutionMixin,
)
from .integrations.vault.write_models import (
    _COMMIT_HASH,
    _MAX_COMMIT_BODY_CHARS,
    _MAX_COMMIT_SUBJECT_CHARS,
    _MAX_WRITE_CONTENT_BYTES,
    _MAX_WRITE_DIFF_CHARS,
    _MAX_WRITE_PATHS,
    DEFAULT_VAULT_COMMIT_EMAIL,
    DEFAULT_VAULT_COMMIT_NAME,
    DEFAULT_VAULT_COMMIT_SUBJECT,
    KNOWLEDGE_VAULT_WRITE_KIND,
    KNOWLEDGE_VAULT_WRITE_SCHEMA,
    VaultCommitIdentity,
    VaultWriteChange,
    VaultWriteDispatchResult,
    VaultWriteProposal,
    VaultWriteRepository,
    VaultWriteRequest,
    _change_from_payload,
    _coerce_change,
    _coerce_identity,
    _parse_write_payload,
)
from .integrations.vault.write_policy import (
    _UNIFIED_HUNK,
    _canonical_email,
    _canonical_note_path,
    _canonical_text,
    _commit_body,
    _commit_hash,
    _normalise_staged_diff,
    _validate_commit_body,
    _validate_commit_subject,
    _validate_note_content,
    _validate_patch,
    canonical_allowed_note_directories,
    render_vault_unified_diff,
    render_vault_write_preview,
)
from .models import FrozenActionProposal
from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ActionDispatchHandle,
)

__all__ = [
    "DEFAULT_VAULT_COMMIT_EMAIL",
    "DEFAULT_VAULT_COMMIT_NAME",
    "DEFAULT_VAULT_COMMIT_SUBJECT",
    "KNOWLEDGE_VAULT_WRITE_KIND",
    "KNOWLEDGE_VAULT_WRITE_SCHEMA",
    "ControlledVaultWriteRepository",
    "KnowledgeVaultWriteConnector",
    "VaultCommitIdentity",
    "VaultWriteChange",
    "VaultWriteConnector",
    "VaultWriteDispatchResult",
    "VaultWriteError",
    "VaultWriteProposal",
    "VaultWritePushPreDispatchFailure",
    "VaultWriteRemoteUnavailable",
    "VaultWriteRepository",
    "VaultWriteRepositoryError",
    "VaultWriteRequest",
    "canonical_allowed_note_directories",
    "render_vault_unified_diff",
    "render_vault_write_preview",
]
