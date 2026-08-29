"""Compatibility re-exports for the knowledge-vault boundary primitives."""

from __future__ import annotations

from .integrations.vault.common import (
    _EXCLUDED_TOP_LEVEL_DIRECTORIES,
    _remaining_seconds,
)

__all__ = ["_EXCLUDED_TOP_LEVEL_DIRECTORIES", "_remaining_seconds"]
