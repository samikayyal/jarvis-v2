"""Release-owned offline database migrations."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .adapters import migrate_sqlite_outbound_conversation_attempts


def _migrate_state(database: Path, applied_at: datetime) -> None:
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(outbound_attempt_record)")
        }
    if "outbound_id" in columns:
        return
    migrate_sqlite_outbound_conversation_attempts(database, applied_at=applied_at)


def _unchanged(_database: Path, _applied_at: datetime) -> None:
    pass


_DATABASES = {
    "state": ("data/state/state.sqlite3", _migrate_state),
    "sessions": ("data/state/sessions.sqlite3", _unchanged),
    "audit": ("data/audit/audit.sqlite3", _unchanged),
    "traces": ("data/traces/traces.sqlite3", _unchanged),
    "codex_traces": ("data/codex_traces/codex.sqlite3", _unchanged),
    "google_traces": ("data/google_traces/google.sqlite3", _unchanged),
    "deleted_conversations": (
        "data/deleted_conversations/deleted-conversations.sqlite3",
        _unchanged,
    ),
}


def migrate_release_databases(root: Path, *, applied_at: datetime) -> None:
    """Apply this release's reviewed migrations to every restored database."""

    databases = {name: root / value[0] for name, value in _DATABASES.items()}
    missing = [name for name, path in databases.items() if not path.is_file()]
    if missing:
        raise ValueError(f"restored databases are missing: {', '.join(missing)}")
    for name, (_, migrate) in _DATABASES.items():
        migrate(databases[name], applied_at)
