"""In-memory and SQLite working-session stores."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ...domain.session_core import HistoryEntry, SessionStoreError, _identifier
from ...domain.session_state import WorkingSession
from ...models import AuditEvidence
from .serialization import _session_from_json, _session_json


class WorkingSessionStore(Protocol):
    """Authoritative current-session state with atomic history writes."""

    def load(self) -> WorkingSession | None: ...

    def create(self, session: WorkingSession) -> None: ...

    def compare_and_set(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        history: Iterable[HistoryEntry] = (),
    ) -> None: ...

    def compare_and_set_with_audit(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        audit: object,
        evidence: AuditEvidence,
        history: Iterable[HistoryEntry] = (),
    ) -> None: ...

    def append_history(self, entry: HistoryEntry) -> None: ...

    def list_history(
        self, session_id: str | None = None
    ) -> tuple[HistoryEntry, ...]: ...


class InMemoryWorkingSessionStore:
    """Thread-safe working-session store used by the composed control plane.

    The compare-and-set boundary is deliberately identical to the SQLite
    adapter so cancellation can race an in-flight orchestration result without
    allowing both transitions to win.
    """

    def __init__(self) -> None:
        self._session: WorkingSession | None = None
        self._history: list[HistoryEntry] = []
        self._lock = threading.RLock()

    def load(self) -> WorkingSession | None:
        with self._lock:
            return self._session

    def create(self, session: WorkingSession) -> None:
        with self._lock:
            if self._session is not None:
                raise SessionStoreError("working session already exists")
            self._session = session

    def compare_and_set(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        history: Iterable[HistoryEntry] = (),
    ) -> None:
        with self._lock:
            if self._session != expected:
                raise SessionStoreError("stale working-session transition")
            entries = tuple(history)
            self._session = updated
            self._history.extend(entries)

    def compare_and_set_with_audit(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        audit: object,
        evidence: AuditEvidence,
        history: Iterable[HistoryEntry] = (),
    ) -> None:
        """Commit state and required evidence as one lock-held in-memory operation."""

        append = getattr(audit, "append", None)
        if not callable(append):
            raise SessionStoreError("audit boundary does not support append")
        with self._lock:
            if self._session != expected:
                raise SessionStoreError("stale working-session transition")
            append(evidence)
            self._session = updated
            self._history.extend(tuple(history))

    def append_history(self, entry: HistoryEntry) -> None:
        with self._lock:
            self._history.append(entry)

    def list_history(self, session_id: str | None = None) -> tuple[HistoryEntry, ...]:
        with self._lock:
            if session_id is None:
                return tuple(self._history)
            return tuple(
                entry for entry in self._history if entry.session_id == session_id
            )


def _locked_working_session_store(
    method: Callable[..., Any],
) -> Callable[..., Any]:
    """Serialize access to the one cross-thread SQLite connection."""

    def locked(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


class SQLiteWorkingSessionStore:
    """SQLite current-session store with complete-state compare-and-set.

    Every transition starts an immediate transaction, compares the canonical
    serialization of the whole previous state, then commits the new state and
    any history entries together. This prevents a stale transition from
    overwriting a newer cancellation or clean-session boundary.
    """

    def __init__(self, database: str | Path | sqlite3.Connection = ":memory:") -> None:
        self._lock = threading.RLock()
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self.connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database), check_same_thread=False)
        )
        try:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS working_session_current (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS working_session_history (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                    body TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    request_id TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS working_session_history_message
                ON working_session_history(session_id, message_id);
                """
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            raise SessionStoreError(
                "could not initialize working-session store"
            ) from exc

    @_locked_working_session_store
    def load(self) -> WorkingSession | None:
        try:
            row = self.connection.execute(
                "SELECT payload FROM working_session_current WHERE slot = 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise SessionStoreError("could not read working session") from exc
        if row is None:
            return None
        restored = _session_from_json(row[0])
        canonical = _session_json(restored)
        if row[0] != canonical:
            try:
                cursor = self.connection.execute(
                    """
                    UPDATE working_session_current SET payload = ?
                    WHERE slot = 1 AND payload = ?
                    """,
                    (canonical, row[0]),
                )
                if cursor.rowcount != 1:
                    # Another process completed the migration or advanced the
                    # session after our read.  Do not return a stale object.
                    self.connection.rollback()
                    return self.load()
                self.connection.commit()
            except sqlite3.Error as exc:
                self.connection.rollback()
                raise SessionStoreError(
                    "could not normalize persisted working session"
                ) from exc
        return restored

    @_locked_working_session_store
    def create(self, session: WorkingSession) -> None:
        try:
            self.connection.execute(
                "INSERT INTO working_session_current(slot, payload) VALUES (1, ?)",
                (_session_json(session),),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise SessionStoreError("could not create working session") from exc

    @_locked_working_session_store
    def compare_and_set(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        history: Iterable[HistoryEntry] = (),
    ) -> None:
        expected_json = _session_json(expected)
        updated_json = _session_json(updated)
        entries = tuple(history)
        if any(not isinstance(entry, HistoryEntry) for entry in entries):
            raise TypeError("history must contain HistoryEntry values")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT payload FROM working_session_current WHERE slot = 1"
            ).fetchone()
            if row is None or row[0] != expected_json:
                self.connection.rollback()
                raise SessionStoreError("stale working-session transition")
            cursor = self.connection.execute(
                """
                UPDATE working_session_current SET payload = ?
                WHERE slot = 1 AND payload = ?
                """,
                (updated_json, expected_json),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise SessionStoreError("stale working-session transition")
            self._append_history_in_transaction(entries)
            self.connection.commit()
        except SessionStoreError:
            raise
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise SessionStoreError(
                "could not compare and set working session"
            ) from exc

    @_locked_working_session_store
    def compare_and_set_with_audit(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        audit: object,
        evidence: AuditEvidence,
        history: Iterable[HistoryEntry] = (),
    ) -> None:
        """Commit session state, outbox, and audit admission in one transaction.

        When the append-only audit shares this SQLite connection, its record is
        written inside the same transaction. Independent audit adapters are
        invoked only while the session transaction is still rollbackable.
        """

        expected_json = _session_json(expected)
        updated_json = _session_json(updated)
        entries = tuple(history)
        append = getattr(audit, "append", None)
        shared_append = getattr(audit, "_append_batch_in_transaction", None)
        if not callable(append):
            raise SessionStoreError("audit boundary does not support append")
        if any(not isinstance(entry, HistoryEntry) for entry in entries):
            raise TypeError("history must contain HistoryEntry values")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT payload FROM working_session_current WHERE slot = 1"
            ).fetchone()
            if row is None or row[0] != expected_json:
                self.connection.rollback()
                raise SessionStoreError("stale working-session transition")
            cursor = self.connection.execute(
                "UPDATE working_session_current SET payload = ? WHERE slot = 1 AND payload = ?",
                (updated_json, expected_json),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise SessionStoreError("stale working-session transition")
            self._append_history_in_transaction(entries)
            if getattr(audit, "_connection", None) is self.connection and callable(
                shared_append
            ):
                shared_append((evidence,))
            else:
                append(evidence)
            self.connection.commit()
        except SessionStoreError:
            raise
        except Exception as exc:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise SessionStoreError(
                "could not atomically commit working-session audit admission"
            ) from exc

    @_locked_working_session_store
    def append_history(self, entry: HistoryEntry) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._append_history_in_transaction((entry,))
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise SessionStoreError("could not append session history") from exc

    def _append_history_in_transaction(self, entries: Iterable[HistoryEntry]) -> None:
        self.connection.executemany(
            """
            INSERT INTO working_session_history(
                session_id, message_id, direction, body, occurred_at, request_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    entry.session_id,
                    entry.message_id,
                    entry.direction,
                    entry.body,
                    entry.occurred_at.isoformat(),
                    entry.request_id,
                )
                for entry in entries
            ),
        )

    @_locked_working_session_store
    def list_history(self, session_id: str | None = None) -> tuple[HistoryEntry, ...]:
        try:
            if session_id is None:
                rows = self.connection.execute(
                    """
                    SELECT session_id, message_id, direction, body, occurred_at, request_id
                    FROM working_session_history ORDER BY sequence
                    """
                ).fetchall()
            else:
                _identifier(session_id, "session_id")
                rows = self.connection.execute(
                    """
                    SELECT session_id, message_id, direction, body, occurred_at, request_id
                    FROM working_session_history WHERE session_id = ? ORDER BY sequence
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise SessionStoreError("could not read session history") from exc
        return tuple(
            HistoryEntry(
                session_id=row[0],
                message_id=row[1],
                direction=row[2],
                body=row[3],
                occurred_at=datetime.fromisoformat(row[4]),
                request_id=row[5],
            )
            for row in rows
        )

    @_locked_working_session_store
    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()
