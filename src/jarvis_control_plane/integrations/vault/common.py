"""Shared primitives used by the vault integration modules."""

from __future__ import annotations

from time import monotonic

_EXCLUDED_TOP_LEVEL_DIRECTORIES = frozenset(
    {"attachments", "plugins", "templates", "themes", "trash"}
)


def _remaining_seconds(deadline: float, error_type: type[Exception]) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise error_type("knowledge-vault operation exceeded its overall deadline")
    return remaining


__all__ = ["_EXCLUDED_TOP_LEVEL_DIRECTORIES", "_remaining_seconds"]
