"""Durable, single-use Google OAuth state and connection state."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ...models import ensure_utc
from .oauth_models import (
    GoogleConnectionState,
    GoogleOAuthError,
    OAuthAuthorization,
    _canonical_scopes,
    _canonical_string,
)


class GoogleOAuthStateStore(Protocol):
    """Durable, single-use state and token-free connection-state boundary."""

    def issue(self, authorization: OAuthAuthorization) -> None: ...

    def consume(self, *, state: str, now: datetime) -> OAuthAuthorization | None: ...

    def set_connection(
        self, *, connected: bool, granted_scopes: frozenset[str] = frozenset()
    ) -> GoogleConnectionState: ...

    def get_connection(self) -> GoogleConnectionState: ...

    def dispatch_lease(self) -> AbstractContextManager[None]: ...

    @property
    def synchronization_lock(self) -> AbstractContextManager[None]: ...


class InMemoryGoogleOAuthStateStore:
    """Thread-safe controlled implementation of the OAuth state boundary."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._authorizations: dict[str, OAuthAuthorization] = {}
        self._connection = GoogleConnectionState()

    def issue(self, authorization: OAuthAuthorization) -> None:
        with self._lock:
            if authorization.state in self._authorizations:
                raise GoogleOAuthError("state_collision")
            self._authorizations[authorization.state] = authorization

    def consume(self, *, state: str, now: datetime) -> OAuthAuthorization | None:
        _canonical_string(state, "state")
        now = ensure_utc(now)
        with self._lock:
            authorization = self._authorizations.pop(state, None)
        if authorization is None or authorization.expires_at <= now:
            return None
        return authorization

    def set_connection(
        self, *, connected: bool, granted_scopes: frozenset[str] = frozenset()
    ) -> GoogleConnectionState:
        if connected:
            granted_scopes = _canonical_scopes(granted_scopes)
        elif granted_scopes:
            raise ValueError("a disconnected state cannot retain granted scopes")
        with self._lock:
            self._connection = GoogleConnectionState(
                connected=connected,
                generation=self._connection.generation + 1,
                granted_scopes=granted_scopes,
            )
            return self._connection

    def get_connection(self) -> GoogleConnectionState:
        with self._lock:
            return self._connection

    @contextmanager
    def dispatch_lease(self) -> Iterator[None]:
        with self._lock:
            yield

    @property
    def synchronization_lock(self) -> AbstractContextManager[None]:
        return self._lock


class SQLiteGoogleOAuthStateStore:
    """SQLite implementation that consumes callback state in one transaction."""

    def __init__(self, database: str | Path | sqlite3.Connection = ":memory:") -> None:
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self.connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database), check_same_thread=False)
        )
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS google_oauth_states (
                    state TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    requested_scopes_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS google_oauth_connection (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    connected INTEGER NOT NULL CHECK (connected IN (0, 1)),
                    generation INTEGER NOT NULL CHECK (generation >= 0),
                    granted_scopes_json TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO google_oauth_connection(
                    slot, connected, generation, granted_scopes_json
                ) VALUES (1, 0, 0, '[]')
                """
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            raise GoogleOAuthError("state_store_unavailable") from exc

    def issue(self, authorization: OAuthAuthorization) -> None:
        try:
            with self._lock:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    """
                    INSERT INTO google_oauth_states(
                        state, operation_id, requested_scopes_json, expires_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        authorization.state,
                        authorization.operation_id,
                        json.dumps(sorted(authorization.requested_scopes)),
                        authorization.expires_at.isoformat(),
                    ),
                )
                self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise GoogleOAuthError("state_collision") from exc
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise GoogleOAuthError("state_store_unavailable") from exc

    def consume(self, *, state: str, now: datetime) -> OAuthAuthorization | None:
        _canonical_string(state, "state")
        now = ensure_utc(now)
        try:
            with self._lock:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    """
                    SELECT operation_id, requested_scopes_json, expires_at
                    FROM google_oauth_states WHERE state = ?
                    """,
                    (state,),
                ).fetchone()
                self.connection.execute(
                    "DELETE FROM google_oauth_states WHERE state = ?", (state,)
                )
                self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise GoogleOAuthError("state_store_unavailable") from exc
        if row is None:
            return None
        try:
            authorization = OAuthAuthorization(
                state=state,
                operation_id=row["operation_id"],
                requested_scopes=frozenset(json.loads(row["requested_scopes_json"])),
                expires_at=datetime.fromisoformat(row["expires_at"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GoogleOAuthError("state_store_unavailable") from exc
        return authorization if authorization.expires_at > now else None

    def set_connection(
        self, *, connected: bool, granted_scopes: frozenset[str] = frozenset()
    ) -> GoogleConnectionState:
        if connected:
            granted_scopes = _canonical_scopes(granted_scopes)
        elif granted_scopes:
            raise ValueError("a disconnected state cannot retain granted scopes")
        try:
            with self._lock:
                self.connection.execute("BEGIN IMMEDIATE")
                generation = self.connection.execute(
                    "SELECT generation FROM google_oauth_connection WHERE slot = 1"
                ).fetchone()["generation"]
                next_state = GoogleConnectionState(
                    connected=connected,
                    generation=generation + 1,
                    granted_scopes=granted_scopes,
                )
                self.connection.execute(
                    """
                    UPDATE google_oauth_connection
                    SET connected = ?, generation = ?, granted_scopes_json = ?
                    WHERE slot = 1
                    """,
                    (
                        int(next_state.connected),
                        next_state.generation,
                        json.dumps(sorted(next_state.granted_scopes)),
                    ),
                )
                self.connection.commit()
                return next_state
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise GoogleOAuthError("state_store_unavailable") from exc

    def get_connection(self) -> GoogleConnectionState:
        try:
            row = self.connection.execute(
                """
                SELECT connected, generation, granted_scopes_json
                FROM google_oauth_connection WHERE slot = 1
                """
            ).fetchone()
        except sqlite3.Error as exc:
            raise GoogleOAuthError("state_store_unavailable") from exc
        if row is None:
            raise GoogleOAuthError("state_store_unavailable")
        try:
            return GoogleConnectionState(
                connected=bool(row["connected"]),
                generation=row["generation"],
                granted_scopes=frozenset(json.loads(row["granted_scopes_json"])),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GoogleOAuthError("state_store_unavailable") from exc

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    @contextmanager
    def dispatch_lease(self) -> Iterator[None]:
        with self._lock:
            yield

    @property
    def synchronization_lock(self) -> AbstractContextManager[None]:
        return self._lock
