"""Layered deleted-conversation archive implementation.

The public control-plane import remains ``conversation_archive`` for
compatibility.  This package holds the independently testable records,
wire, memory, SQLite, and service layers behind that facade.
"""

from .memory import InMemoryDeletedConversationArchive
from .records import DeletedConversationArchiveRecord
from .service import (
    SQLiteDeletedConversationArchiveService,
    serve_archive,
    start_archive_service,
)
from .sqlite_writer import SQLiteDeletedConversationArchiveWriter

__all__ = [
    "DeletedConversationArchiveRecord",
    "InMemoryDeletedConversationArchive",
    "SQLiteDeletedConversationArchiveService",
    "SQLiteDeletedConversationArchiveWriter",
    "serve_archive",
    "start_archive_service",
]
