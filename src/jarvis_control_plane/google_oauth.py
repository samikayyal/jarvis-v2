"""State-bound Google OAuth lifecycle with a connector-only credential boundary.

This module owns the narrow public callback seam.  It deliberately accepts no
Jarvis commands or action proposals: a valid callback can only replace the
Google connector's credential record for the configured Google subject.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from .models import AuditEvidence, ensure_utc
from .ports import (
    AuditBoundary,
    AuditWriteError,
    Clock,
    DiagnosticTraceError,
    IdGenerator,
    TraceWriteError,
)
from .traces import DiagnosticTraceRecorder

GOOGLE_OAUTH_STATE_TTL = timedelta(minutes=10)
GOOGLE_OAUTH_TRACE_PAYLOAD_LIMIT_BYTES = 32 * 1024
GOOGLE_OAUTH_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/drive.readonly",
    }
)
_CALLBACK_FIELDS = frozenset(
    {"state", "code", "scope", "error", "error_description", "error_uri"}
)


class GoogleOAuthError(RuntimeError):
    """A bounded Google OAuth lifecycle failure safe to expose as an HTTP code."""


class OAuthExchangeError(GoogleOAuthError):
    """The connector could not turn one authorization code into a usable grant."""

    _CODES = frozenset(
        {
            "invalid_grant",
            "wrong_identity",
            "missing_scope",
            "missing_refresh_token",
            "provider_failure",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("OAuth exchange failures must use a controlled code")
        super().__init__(code)
        self.code = code


def _canonical_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty canonical string")
    return value


def _canonical_scopes(scopes: Sequence[str] | frozenset[str]) -> frozenset[str]:
    values = frozenset(_canonical_string(scope, "scope") for scope in scopes)
    if not values:
        raise ValueError("at least one OAuth scope is required")
    if not values <= GOOGLE_OAUTH_SCOPES:
        raise ValueError("OAuth scope is outside the exact Google connector allowlist")
    return values


@dataclass(frozen=True, slots=True)
class OAuthAuthorization:
    """Non-secret, short-lived callback state bound to its initiating operation."""

    state: str
    operation_id: str
    requested_scopes: frozenset[str]
    expires_at: datetime

    def __post_init__(self) -> None:
        _canonical_string(self.state, "state")
        _canonical_string(self.operation_id, "operation_id")
        object.__setattr__(
            self, "requested_scopes", _canonical_scopes(self.requested_scopes)
        )
        object.__setattr__(self, "expires_at", ensure_utc(self.expires_at))


@dataclass(frozen=True, slots=True)
class OAuthGrant:
    """One provider exchange result; token fields never leave the connector."""

    subject: str
    granted_scopes: frozenset[str]
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)

    def __post_init__(self) -> None:
        _canonical_string(self.subject, "subject")
        object.__setattr__(
            self, "granted_scopes", _canonical_scopes(self.granted_scopes)
        )
        _canonical_string(self.access_token, "access_token")
        _canonical_string(self.refresh_token, "refresh_token")


@dataclass(frozen=True, slots=True)
class OAuthCredentialRecord:
    """The sole durable Google credential record, owned by the connector."""

    subject: str
    granted_scopes: frozenset[str]
    refresh_token: str = field(repr=False)
    connection_generation: int = 0

    def __post_init__(self) -> None:
        _canonical_string(self.subject, "subject")
        object.__setattr__(
            self, "granted_scopes", _canonical_scopes(self.granted_scopes)
        )
        _canonical_string(self.refresh_token, "refresh_token")
        if (
            not isinstance(self.connection_generation, int)
            or isinstance(self.connection_generation, bool)
            or self.connection_generation < 0
        ):
            raise ValueError("connection_generation must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class GoogleConnectionState:
    """Token-free connection state used to invalidate later bound actions."""

    connected: bool = False
    generation: int = 0
    granted_scopes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.connected, bool):
            raise TypeError("connected must be a boolean")
        if not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        scopes = frozenset(self.granted_scopes)
        if self.connected:
            object.__setattr__(self, "granted_scopes", _canonical_scopes(scopes))
        elif scopes:
            raise ValueError("a disconnected state cannot retain granted scopes")


@dataclass(frozen=True, slots=True)
class OAuthCallbackResponse:
    """A deliberately content-free response from the only public endpoint."""

    status_code: int
    body: bytes = b""
    headers: Mapping[str, str] = field(
        default_factory=lambda: {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Length": "0",
        }
    )

    def __post_init__(self) -> None:
        if self.status_code not in {204, 400, 405, 503}:
            raise ValueError("callback responses must use one controlled status")
        if self.body != b"":
            raise ValueError("OAuth callback responses must be content-free")


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


class GoogleCredentialStore(Protocol):
    """Private connector-owned persistence for the sole refresh-token record."""

    @property
    def current(self) -> OAuthCredentialRecord | None: ...

    def replace(self, credential: OAuthCredentialRecord) -> None: ...

    def delete(self) -> None: ...


@dataclass(frozen=True, slots=True)
class GoogleConnectionSnapshot:
    """One lock-consistent view of connection state and its credential."""

    connection: GoogleConnectionState
    credential: OAuthCredentialRecord | None


class GoogleConnectionBinding:
    """Shared synchronization boundary for OAuth state and credential changes."""

    def __init__(
        self,
        *,
        state_store: GoogleOAuthStateStore,
        credential_store: GoogleCredentialStore,
    ) -> None:
        self._state_store = state_store
        self._credential_store = credential_store
        self.synchronization_lock = state_store.synchronization_lock

    def snapshot(self) -> GoogleConnectionSnapshot:
        """Read generation and credential while no lifecycle mutation can interleave."""

        with self.synchronization_lock:
            return GoogleConnectionSnapshot(
                connection=self._state_store.get_connection(),
                credential=self._credential_store.current,
            )


class InMemoryGoogleCredentialStore:
    """Controlled credential-store double; it is never an ordinary state store."""

    def __init__(self, current: OAuthCredentialRecord | None = None) -> None:
        self._current = current

    @property
    def current(self) -> OAuthCredentialRecord | None:
        return self._current

    def replace(self, credential: OAuthCredentialRecord) -> None:
        self._current = credential

    def delete(self) -> None:
        self._current = None


class FileGoogleCredentialStore:
    """One 0600 credential file replaced atomically within a private directory."""

    def __init__(
        self, directory: str | Path, *, filename: str = "google-oauth.json"
    ) -> None:
        self._directory = Path(directory)
        self._filename = _canonical_string(filename, "filename")
        if Path(self._filename).name != self._filename:
            raise ValueError("credential filename must not contain a path")
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._restrict_directory()

    @property
    def _path(self) -> Path:
        return self._directory / self._filename

    @property
    def _metadata_path(self) -> Path:
        return self._directory / f"{self._filename}.meta"

    def _restrict_directory(self) -> None:
        if os.name != "nt":
            os.chmod(self._directory, 0o700)

    @property
    def current(self) -> OAuthCredentialRecord | None:
        if not self._path.exists():
            return None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            fields = set(payload)
            if fields == {"granted_scopes", "refresh_token", "subject"}:
                connection_generation = self._legacy_generation(payload)
            elif fields == {
                "connection_generation",
                "granted_scopes",
                "refresh_token",
                "subject",
            }:
                credential = OAuthCredentialRecord(
                    subject=payload["subject"],
                    granted_scopes=frozenset(payload["granted_scopes"]),
                    refresh_token=payload["refresh_token"],
                    connection_generation=payload["connection_generation"],
                )
                self._write_legacy_compatible_record(credential)
                return credential
            else:
                raise ValueError("credential record has an unexpected shape")
            return OAuthCredentialRecord(
                subject=payload["subject"],
                granted_scopes=frozenset(payload["granted_scopes"]),
                refresh_token=payload["refresh_token"],
                connection_generation=connection_generation,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise GoogleOAuthError("credential_store_unavailable") from exc

    def replace(self, credential: OAuthCredentialRecord) -> None:
        self._write_legacy_compatible_record(credential)

    def _write_legacy_compatible_record(
        self, credential: OAuthCredentialRecord
    ) -> None:
        previous = self._read_existing_record()
        records = []
        if previous is not None and previous.refresh_token != credential.refresh_token:
            records.append(self._metadata_record(previous))
        records.append(self._metadata_record(credential))
        metadata = {
            "records": records,
            "schema": "google_oauth_credential_metadata_v2",
        }
        payload = {
            "granted_scopes": sorted(credential.granted_scopes),
            "refresh_token": credential.refresh_token,
            "subject": credential.subject,
        }
        self._atomic_write(self._metadata_path, metadata)
        self._atomic_write(self._path, payload)

    def _read_existing_record(self) -> OAuthCredentialRecord | None:
        if not self._path.exists():
            return None
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        fields = set(payload)
        if fields == {"granted_scopes", "refresh_token", "subject"}:
            connection_generation = self._legacy_generation(payload)
        elif fields == {
            "connection_generation",
            "granted_scopes",
            "refresh_token",
            "subject",
        }:
            connection_generation = payload["connection_generation"]
        else:
            raise ValueError("credential record has an unexpected shape")
        return OAuthCredentialRecord(
            subject=payload["subject"],
            granted_scopes=frozenset(payload["granted_scopes"]),
            refresh_token=payload["refresh_token"],
            connection_generation=connection_generation,
        )

    @staticmethod
    def _metadata_record(credential: OAuthCredentialRecord) -> dict[str, object]:
        return {
            "connection_generation": credential.connection_generation,
            "refresh_token_sha256": hashlib.sha256(
                credential.refresh_token.encode("utf-8")
            ).hexdigest(),
        }

    def _legacy_generation(self, payload: Mapping[str, object]) -> int:
        if not self._metadata_path.exists():
            return 0
        metadata = json.loads(self._metadata_path.read_text(encoding="utf-8"))
        refresh_token = payload.get("refresh_token")
        if not isinstance(refresh_token, str):
            raise TypeError("credential record has an invalid refresh token")
        fingerprint = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        if (
            isinstance(metadata, Mapping)
            and set(metadata)
            == {"connection_generation", "refresh_token_sha256", "schema"}
            and metadata["schema"] == "google_oauth_credential_metadata_v1"
        ):
            if metadata["refresh_token_sha256"] != fingerprint:
                return 0
            return int(metadata["connection_generation"])
        if (
            not isinstance(metadata, Mapping)
            or set(metadata) != {"records", "schema"}
            or metadata["schema"] != "google_oauth_credential_metadata_v2"
            or not isinstance(metadata["records"], list)
            or not metadata["records"]
        ):
            raise ValueError("credential metadata has an unexpected shape")
        matches = [
            record
            for record in metadata["records"]
            if isinstance(record, Mapping)
            and set(record) == {"connection_generation", "refresh_token_sha256"}
            and record["refresh_token_sha256"] == fingerprint
        ]
        if not matches:
            return 0
        generations = {record["connection_generation"] for record in matches}
        if len(generations) != 1:
            raise ValueError("credential metadata has conflicting generations")
        generation = next(iter(generations))
        if not isinstance(generation, int):
            raise TypeError("credential metadata has an invalid generation")
        return generation

    def _atomic_write(self, path: Path, payload: Mapping[str, object]) -> None:
        temporary = self._directory / f".{path.name}.{secrets.token_hex(16)}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                os.chmod(path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def delete(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
            self._metadata_path.unlink(missing_ok=True)
        except OSError as exc:
            raise GoogleOAuthError("credential_store_unavailable") from exc


class GoogleOAuthProvider(Protocol):
    """The narrow live-provider edge; no generic Google methods are exposed here."""

    def exchange_code(
        self, *, code: str, requested_scopes: frozenset[str]
    ) -> OAuthGrant: ...

    def revoke(self, *, refresh_token: str) -> None: ...


class ControlledGoogleOAuthProvider:
    """Controlled OAuth double that never contacts Google."""

    def __init__(
        self,
        *,
        grant: OAuthGrant,
        exchange_failure: str | None = None,
        revoke_failure: str | None = None,
    ) -> None:
        if exchange_failure is not None:
            OAuthExchangeError(exchange_failure)
        self.grant = grant
        self.exchange_failure = exchange_failure
        self.revoke_failure = revoke_failure
        self.exchange_calls: list[tuple[str, frozenset[str]]] = []
        self.revoke_calls: list[str] = []

    def exchange_code(
        self, *, code: str, requested_scopes: frozenset[str]
    ) -> OAuthGrant:
        _canonical_string(code, "code")
        self.exchange_calls.append((code, requested_scopes))
        if self.exchange_failure is not None:
            raise OAuthExchangeError(self.exchange_failure)
        return self.grant

    def revoke(self, *, refresh_token: str) -> None:
        _canonical_string(refresh_token, "refresh_token")
        self.revoke_calls.append(refresh_token)
        if self.revoke_failure is not None:
            raise GoogleOAuthError("provider_failure")


@dataclass(frozen=True, slots=True)
class _ExchangeReceipt:
    """Complete connector result retained only in the diagnostic trace boundary."""

    grant: OAuthGrant


@dataclass(frozen=True, slots=True)
class _RevocationReceipt:
    """Complete revocation input and result retained only in diagnostic traces."""

    credential: OAuthCredentialRecord | None
    provider_result: None = None


@dataclass(slots=True)
class _CallbackExchangeContext:
    previous_generation: int | None = None
    next_generation: int | None = None


class GoogleOAuthConnector:
    """Exchange code and atomically own refresh-token replacement in one boundary."""

    def __init__(
        self,
        *,
        configured_identity: str,
        provider: GoogleOAuthProvider,
        credential_store: GoogleCredentialStore,
    ) -> None:
        self._configured_identity = _canonical_string(
            configured_identity, "configured_identity"
        )
        self._provider = provider
        self._credential_store = credential_store

    def exchange_and_replace(
        self,
        *,
        code: str,
        requested_scopes: frozenset[str],
        connection_generation: int = 0,
    ) -> _ExchangeReceipt:
        grant = self._provider.exchange_code(
            code=code, requested_scopes=requested_scopes
        )
        if grant.subject != self._configured_identity:
            raise OAuthExchangeError("wrong_identity")
        if not requested_scopes <= grant.granted_scopes:
            raise OAuthExchangeError("missing_scope")
        if not grant.refresh_token:
            raise OAuthExchangeError("missing_refresh_token")
        self._credential_store.replace(
            OAuthCredentialRecord(
                subject=grant.subject,
                granted_scopes=grant.granted_scopes,
                refresh_token=grant.refresh_token,
                connection_generation=connection_generation,
            )
        )
        return _ExchangeReceipt(grant=grant)

    @property
    def current_credential(self) -> OAuthCredentialRecord | None:
        """Expose the current credential only to the isolated trace call site."""

        return self._credential_store.current

    def disconnect(self) -> _RevocationReceipt:
        credential = self._credential_store.current
        if credential is None:
            return _RevocationReceipt(credential=None)
        try:
            self._provider.revoke(refresh_token=credential.refresh_token)
        finally:
            self._credential_store.delete()
        return _RevocationReceipt(credential=credential)

    def discard_local_credential(self) -> None:
        self._credential_store.delete()


class GoogleOAuthLifecycle:
    """Public-callback handler and state lifecycle for one configured identity."""

    def __init__(
        self,
        *,
        configured_identity: str,
        state_store: GoogleOAuthStateStore,
        credential_store: GoogleCredentialStore,
        provider: GoogleOAuthProvider,
        audit: AuditBoundary,
        trace: DiagnosticTraceRecorder,
        clock: Clock,
        ids: IdGenerator,
        state_factory: Callable[[], str] | None = None,
        state_ttl: timedelta = GOOGLE_OAUTH_STATE_TTL,
    ) -> None:
        self._configured_identity = _canonical_string(
            configured_identity, "configured_identity"
        )
        if state_ttl <= timedelta() or state_ttl > GOOGLE_OAUTH_STATE_TTL:
            raise ValueError(
                "OAuth state TTL must be positive and no longer than ten minutes"
            )
        self._state_store = state_store
        self._connection_binding = GoogleConnectionBinding(
            state_store=state_store,
            credential_store=credential_store,
        )
        self._connector = GoogleOAuthConnector(
            configured_identity=self._configured_identity,
            provider=provider,
            credential_store=credential_store,
        )
        self._audit = audit
        self._trace = trace
        self._clock = clock
        self._ids = ids
        self._state_factory = state_factory or (lambda: secrets.token_urlsafe(32))
        self._state_ttl = state_ttl

    @property
    def connection(self) -> GoogleConnectionState:
        return self._connection_binding.snapshot().connection

    @property
    def connection_binding(self) -> GoogleConnectionBinding:
        """Expose the shared lifecycle boundary to connector composition only."""

        return self._connection_binding

    def start_authorization(
        self, *, operation_id: str, requested_scopes: Sequence[str]
    ) -> OAuthAuthorization:
        operation_id = _canonical_string(operation_id, "operation_id")
        scopes = _canonical_scopes(requested_scopes)
        self._append_audit(
            kind="google_oauth_authorization_started",
            request_id=operation_id,
            outcome="accepted",
            execution_status="accepted",
        )
        state = _canonical_string(self._state_factory(), "state")
        authorization = OAuthAuthorization(
            state=state,
            operation_id=operation_id,
            requested_scopes=scopes,
            expires_at=self._clock.now() + self._state_ttl,
        )
        self._state_store.issue(authorization)
        return authorization

    def handle_callback(
        self, *, method: str, query: Mapping[str, str]
    ) -> OAuthCallbackResponse:
        if method != "GET":
            return OAuthCallbackResponse(status_code=405)
        if not self._valid_callback_query(query):
            return OAuthCallbackResponse(status_code=400)
        state = query["state"]
        try:
            authorization = self._state_store.consume(
                state=state, now=self._clock.now()
            )
        except GoogleOAuthError:
            return OAuthCallbackResponse(status_code=503)
        if authorization is None:
            return OAuthCallbackResponse(status_code=400)
        if "error" in query:
            self._append_callback_rejection(authorization.operation_id)
            return OAuthCallbackResponse(status_code=400)

        try:
            self._append_audit(
                kind="google_oauth_code_exchange_started",
                request_id=authorization.operation_id,
                outcome="attempted",
                execution_status="attempted",
            )
        except AuditWriteError:
            return OAuthCallbackResponse(status_code=503)

        context = _CallbackExchangeContext()
        try:
            self._exchange_and_publish(
                authorization=authorization,
                code=query["code"],
                context=context,
            )
        except OAuthExchangeError as exc:
            if self._has_trace_write_failure(exc):
                self._invalidate_connection(
                    connection_generation=context.next_generation,
                    previous_generation=context.previous_generation,
                )
                return OAuthCallbackResponse(status_code=503)
            self._append_callback_rejection(authorization.operation_id)
            return OAuthCallbackResponse(status_code=400)
        except (DiagnosticTraceError, GoogleOAuthError, OSError):
            # A failed trace reservation or write makes the exchange outcome
            # unusable.  Delete the private credential and force reconnect.
            self._invalidate_connection(
                connection_generation=context.next_generation,
                previous_generation=context.previous_generation,
            )
            return OAuthCallbackResponse(status_code=503)

        try:
            self._append_audit(
                kind="google_oauth_code_exchange_completed",
                request_id=authorization.operation_id,
                outcome="connected",
                execution_status="completed",
            )
        except AuditWriteError:
            self._invalidate_connection(connection_generation=context.next_generation)
            return OAuthCallbackResponse(status_code=503)
        return OAuthCallbackResponse(status_code=204)

    def _exchange_and_publish(
        self,
        *,
        authorization: OAuthAuthorization,
        code: str,
        context: _CallbackExchangeContext,
    ) -> None:
        with self._connection_binding.synchronization_lock:
            context.previous_generation = self.connection.generation
            context.next_generation = context.previous_generation + 1
            receipt = self._trace.execute(
                request_id=authorization.operation_id,
                operation_type="google_oauth_code_exchange",
                operation=lambda: self._connector.exchange_and_replace(
                    code=code,
                    requested_scopes=authorization.requested_scopes,
                    connection_generation=context.next_generation,
                ),
                arguments={
                    "flow": "authorization_code",
                    "authorization_code": code,
                    "requested_scopes": authorization.requested_scopes,
                },
                result_limit_bytes=GOOGLE_OAUTH_TRACE_PAYLOAD_LIMIT_BYTES,
                error_limit_bytes=GOOGLE_OAUTH_TRACE_PAYLOAD_LIMIT_BYTES,
            )
            self._state_store.set_connection(
                connected=True, granted_scopes=receipt.grant.granted_scopes
            )

    def handle_refresh_failure(
        self, error_code: str, *, connection_generation: int | None = None
    ) -> GoogleConnectionState:
        if error_code != "invalid_grant":
            raise ValueError("only invalid_grant can invalidate the OAuth credential")
        if connection_generation is not None and (
            not isinstance(connection_generation, int)
            or isinstance(connection_generation, bool)
            or connection_generation < 0
        ):
            raise ValueError("connection_generation must be a non-negative integer")
        with self._connection_binding.synchronization_lock:
            current = self._state_store.get_connection()
            credential = self._connector.current_credential
            if connection_generation is None or (
                current.generation == connection_generation
                and credential is not None
                and credential.connection_generation == connection_generation
            ):
                self._connector.discard_local_credential()
                state = self._state_store.set_connection(connected=False)
                outcome = "invalidated"
            else:
                state = current
                outcome = "stale_ignored"
        self._append_audit(
            kind="google_oauth_refresh_invalidated",
            request_id="google-oauth-refresh",
            outcome=outcome,
            execution_status="failed" if outcome == "invalidated" else "ignored",
        )
        return state

    def disconnect(self) -> GoogleConnectionState:
        self._append_audit(
            kind="google_oauth_revocation_started",
            request_id="google-oauth-disconnect",
            outcome="attempted",
            execution_status="attempted",
        )
        with self._connection_binding.synchronization_lock:
            credential = self._connector.current_credential
            try:
                self._trace.execute(
                    request_id="google-oauth-disconnect",
                    operation_type="google_oauth_revocation",
                    operation=self._connector.disconnect,
                    arguments={
                        "flow": "revocation",
                        "refresh_token": (
                            credential.refresh_token if credential is not None else None
                        ),
                    },
                    result_limit_bytes=GOOGLE_OAUTH_TRACE_PAYLOAD_LIMIT_BYTES,
                    error_limit_bytes=GOOGLE_OAUTH_TRACE_PAYLOAD_LIMIT_BYTES,
                )
            finally:
                state = self._state_store.set_connection(connected=False)
        self._append_audit(
            kind="google_oauth_revocation_completed",
            request_id="google-oauth-disconnect",
            outcome="disconnected",
            execution_status="completed",
        )
        return state

    def _append_callback_rejection(self, request_id: str) -> None:
        try:
            self._append_audit(
                kind="google_oauth_callback_rejected",
                request_id=request_id,
                outcome="rejected",
                execution_status="rejected",
            )
        except AuditWriteError:
            # A rejected callback performs no connector work, so only its
            # content-free response remains available while audit is down.
            pass

    def _invalidate_connection(
        self,
        *,
        connection_generation: int | None,
        previous_generation: int | None = None,
    ) -> None:
        """Remove a possibly replaced credential without leaking state-store failures."""

        if connection_generation is None:
            return
        with self._connection_binding.synchronization_lock:
            try:
                current = self._state_store.get_connection()
                credential = self._connector.current_credential
            except GoogleOAuthError:
                return
            if credential is None or (
                credential.connection_generation != connection_generation
            ):
                return
            if current.generation == connection_generation:
                should_disconnect = True
            elif (
                previous_generation is not None
                and current.generation == previous_generation
            ):
                should_disconnect = False
            else:
                return
            try:
                self._connector.discard_local_credential()
            except GoogleOAuthError:
                pass
            if should_disconnect:
                try:
                    self._state_store.set_connection(connected=False)
                except GoogleOAuthError:
                    pass

    @staticmethod
    def _has_trace_write_failure(error: BaseException) -> bool:
        """Keep degraded trace retention distinct from an ordinary OAuth rejection."""

        cause = error.__cause__
        while cause is not None:
            if isinstance(cause, TraceWriteError):
                return True
            cause = cause.__cause__
        return False

    def _append_audit(
        self,
        *,
        kind: str,
        request_id: str,
        outcome: str,
        execution_status: str,
    ) -> None:
        self._audit.append(
            AuditEvidence(
                evidence_id=self._ids.new_id("audit"),
                kind=kind,
                occurred_at=self._clock.now(),
                request_id=request_id,
                operation_type="google_oauth",
                target_category="google_connector",
                actor="google_connector",
                outcome=outcome,
                execution_status=execution_status,
            )
        )

    @staticmethod
    def _valid_callback_query(query: Mapping[str, str]) -> bool:
        if not isinstance(query, Mapping) or set(query) - _CALLBACK_FIELDS:
            return False
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not value
            or value.strip() != value
            for key, value in query.items()
        ):
            return False
        state = query.get("state")
        if state is None:
            return False
        has_code = "code" in query
        has_error = "error" in query
        if has_code == has_error:
            return False
        if has_code and ({"error_description", "error_uri"} & set(query)):
            return False
        return not (has_error and "scope" in query)
