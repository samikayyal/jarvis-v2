"""State-bound Google OAuth lifecycle and public callback seam."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from ...models import AuditEvidence
from ...ports import (
    AuditBoundary,
    AuditWriteError,
    Clock,
    DiagnosticTraceError,
    IdGenerator,
    TraceWriteError,
)
from ...traces import DiagnosticTraceRecorder
from .credentials import GoogleConnectionBinding, GoogleCredentialStore
from .oauth_connector import GoogleOAuthConnector
from .oauth_models import (
    _CALLBACK_FIELDS,
    _GOOGLE_AUTHORIZATION_ISSUER,
    GOOGLE_OAUTH_SCOPES,
    GOOGLE_OAUTH_STATE_TTL,
    GOOGLE_OAUTH_TRACE_PAYLOAD_LIMIT_BYTES,
    GoogleConnectionState,
    GoogleOAuthError,
    OAuthAuthorization,
    OAuthCallbackResponse,
    OAuthExchangeError,
    _canonical_scopes,
    _canonical_string,
)
from .oauth_provider import GoogleOAuthProvider
from .oauth_state import GoogleOAuthStateStore


@dataclass(slots=True)
class _CallbackExchangeContext:
    previous_generation: int | None = None
    next_generation: int | None = None


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
        if not scopes <= GOOGLE_OAUTH_SCOPES:
            raise ValueError("OAuth scope is outside the Jarvis v1 request surface")
        connection = self.connection
        if connection.connected:
            # An action-specific consent adds one capability without silently
            # dropping another in-scope grant from the reviewed connection.
            retained = connection.granted_scopes & GOOGLE_OAUTH_SCOPES
            scopes = _canonical_scopes(scopes | retained)
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
            # unusable. Delete the private credential and force reconnect.
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
        issuer = query.get("iss")
        if issuer is not None and issuer != _GOOGLE_AUTHORIZATION_ISSUER:
            return False
        authuser = query.get("authuser")
        if authuser is not None and (
            len(authuser) > 3 or not authuser.isascii() or not authuser.isdecimal()
        ):
            return False
        prompt = query.get("prompt")
        if prompt is not None and prompt != "consent":
            return False
        has_code = "code" in query
        has_error = "error" in query
        if has_code == has_error:
            return False
        if has_code and ({"error_description", "error_uri"} & set(query)):
            return False
        return not (has_error and "scope" in query)
