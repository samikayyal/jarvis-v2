"""Shared boundary primitives for the read and write knowledge-vault edges."""

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
