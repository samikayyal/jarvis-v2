"""Compatibility facade for the production knowledge-vault repository edge."""

from __future__ import annotations

# ``run`` remains a facade-level seam for older tests that replaced the Git
# process runner by monkeypatching this module.
# ruff: noqa: F401
import os
import shlex
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired, run
from tempfile import TemporaryDirectory

from .integrations.vault.common import _remaining_seconds
from .integrations.vault.errors import (
    VaultPushPreDispatchFailure,
    VaultPushUnknownOutcome,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
    VaultWriteConflict,
    VaultWriteRemoteUnavailable,
    VaultWriteRepositoryError,
)
from .integrations.vault.repository import SubprocessVaultRepository as _Repository


class SubprocessVaultRepository(_Repository):
    """Legacy class name with the historical module-level runner seam."""

    def _run_process(
        self, command: Sequence[str], **kwargs: object
    ) -> CompletedProcess[str]:
        check = kwargs.pop("check", False)
        return run(command, check=check, **kwargs)  # type: ignore[arg-type]


__all__ = ["SubprocessVaultRepository"]
