"""Values and invariants shared by the Google OAuth lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ...models import ensure_utc

GOOGLE_OAUTH_STATE_TTL = timedelta(minutes=10)
GOOGLE_OAUTH_TRACE_PAYLOAD_LIMIT_BYTES = 32 * 1024
GOOGLE_OAUTH_BASELINE_SCOPES = frozenset(
    {
        "openid",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    }
)
GOOGLE_OAUTH_SCOPES = GOOGLE_OAUTH_BASELINE_SCOPES | frozenset(
    {"https://www.googleapis.com/auth/gmail.send"}
)
_CALLBACK_FIELDS = frozenset(
    {
        "state",
        "code",
        "scope",
        "error",
        "error_description",
        "error_uri",
        "iss",
        "authuser",
        "prompt",
    }
)
_GOOGLE_AUTHORIZATION_ISSUER = "https://accounts.google.com"


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
            "invalid_provider_response",
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
