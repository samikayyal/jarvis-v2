"""Stable failure types for the bounded knowledge-vault integration."""

from __future__ import annotations


class VaultReadError(Exception):
    """A vault read was invalid, unavailable, or outside its boundary."""

    _default_code = "read_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        selected_code = code or self._default_code
        if not selected_code or selected_code.strip() != selected_code:
            raise ValueError("vault read error code must be canonical")
        self.code = selected_code


class VaultSynchronizationError(VaultReadError):
    """The dedicated clone could not be synchronized safely."""

    _default_code = "synchronization_failed"


class VaultRemoteUnavailable(VaultSynchronizationError):
    """The remote could not be reached; a known clean clone may be read stale."""

    _default_code = "remote_unavailable"


class VaultRepositoryConflict(VaultSynchronizationError):
    """The local clone requires explicit administrator recovery."""

    _default_code = "recovery_required"


class VaultPushPreDispatchFailure(VaultSynchronizationError):
    """The push process did not start, so no remote update could have occurred."""

    _default_code = "push_not_started"


class VaultPushUnknownOutcome(VaultSynchronizationError):
    """The push process started, but its remote side effect is not established."""

    _default_code = "push_outcome_unknown"


class VaultWriteError(Exception):
    """A vault write was invalid, unavailable, or outside its boundary."""


class VaultWriteRemoteUnavailable(VaultWriteError):
    """The remote could not be reached while a write was being prepared."""


class VaultWritePushPreDispatchFailure(VaultWriteError):
    """A bounded push retry ended before the push process ever started."""


class VaultWriteConflict(VaultWriteError):
    """The exact approved write no longer matches repository state."""


class VaultWriteRepositoryError(VaultWriteError):
    """The repository edge could not safely complete a write operation."""


__all__ = [
    "VaultPushPreDispatchFailure",
    "VaultPushUnknownOutcome",
    "VaultReadError",
    "VaultRemoteUnavailable",
    "VaultRepositoryConflict",
    "VaultSynchronizationError",
    "VaultWriteConflict",
    "VaultWriteError",
    "VaultWritePushPreDispatchFailure",
    "VaultWriteRemoteUnavailable",
    "VaultWriteRepositoryError",
]
