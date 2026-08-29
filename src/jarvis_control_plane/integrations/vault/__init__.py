"""Knowledge-vault integration grouped by read, write, and Git boundaries."""

from .errors import (
    VaultPushPreDispatchFailure,
    VaultPushUnknownOutcome,
    VaultReadError,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
    VaultSynchronizationError,
    VaultWriteConflict,
    VaultWriteError,
    VaultWritePushPreDispatchFailure,
    VaultWriteRemoteUnavailable,
    VaultWriteRepositoryError,
)
from .read_connector import KnowledgeVaultConnector
from .read_models import (
    KnowledgeVaultReadResult,
    VaultExcerpt,
    VaultReadInput,
    VaultSynchronizationMetadataStore,
    VaultSynchronizer,
)
from .repository import ControlledVaultWriteRepository, SubprocessVaultRepository
from .synchronizer import ControlledVaultSynchronizer, SubprocessVaultSynchronizer
from .write_connector import KnowledgeVaultWriteConnector, VaultWriteConnector
from .write_models import (
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
)
from .write_policy import (
    canonical_allowed_note_directories,
    render_vault_unified_diff,
    render_vault_write_preview,
)

__all__ = [
    "DEFAULT_VAULT_COMMIT_EMAIL",
    "DEFAULT_VAULT_COMMIT_NAME",
    "DEFAULT_VAULT_COMMIT_SUBJECT",
    "KNOWLEDGE_VAULT_WRITE_KIND",
    "KNOWLEDGE_VAULT_WRITE_SCHEMA",
    "ControlledVaultSynchronizer",
    "ControlledVaultWriteRepository",
    "KnowledgeVaultConnector",
    "KnowledgeVaultReadResult",
    "KnowledgeVaultWriteConnector",
    "SubprocessVaultRepository",
    "SubprocessVaultSynchronizer",
    "VaultCommitIdentity",
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
    "VaultWriteChange",
    "VaultWriteConflict",
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
