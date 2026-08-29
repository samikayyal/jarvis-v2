"""Compatibility facade for the state-bound Google OAuth integration."""

# These imports intentionally preserve direct access to the former module's
# implementation names for compatibility with existing connector composition.
# ruff: noqa: F401
import os

from .integrations.google.credentials import (
    FileGoogleCredentialStore,
    GoogleConnectionBinding,
    GoogleConnectionSnapshot,
    GoogleCredentialStore,
    InMemoryGoogleCredentialStore,
)
from .integrations.google.oauth_connector import (
    GoogleOAuthConnector,
    _ExchangeReceipt,
    _RevocationReceipt,
)
from .integrations.google.oauth_lifecycle import (
    GoogleOAuthLifecycle,
    _CallbackExchangeContext,
)
from .integrations.google.oauth_models import (
    _CALLBACK_FIELDS,
    _GOOGLE_AUTHORIZATION_ISSUER,
    GOOGLE_OAUTH_BASELINE_SCOPES,
    GOOGLE_OAUTH_SCOPES,
    GOOGLE_OAUTH_STATE_TTL,
    GOOGLE_OAUTH_TRACE_PAYLOAD_LIMIT_BYTES,
    GoogleConnectionState,
    GoogleOAuthError,
    OAuthAuthorization,
    OAuthCallbackResponse,
    OAuthCredentialRecord,
    OAuthExchangeError,
    OAuthGrant,
    _canonical_scopes,
    _canonical_string,
)
from .integrations.google.oauth_provider import (
    ControlledGoogleOAuthProvider,
    GoogleLiveOAuthProvider,
    GoogleOAuthProvider,
    _RejectOAuthRedirects,
)
from .integrations.google.oauth_state import (
    GoogleOAuthStateStore,
    InMemoryGoogleOAuthStateStore,
    SQLiteGoogleOAuthStateStore,
)

__all__ = [
    "GOOGLE_OAUTH_BASELINE_SCOPES",
    "GOOGLE_OAUTH_SCOPES",
    "GOOGLE_OAUTH_STATE_TTL",
    "GOOGLE_OAUTH_TRACE_PAYLOAD_LIMIT_BYTES",
    "ControlledGoogleOAuthProvider",
    "FileGoogleCredentialStore",
    "GoogleConnectionBinding",
    "GoogleConnectionSnapshot",
    "GoogleConnectionState",
    "GoogleCredentialStore",
    "GoogleLiveOAuthProvider",
    "GoogleOAuthConnector",
    "GoogleOAuthError",
    "GoogleOAuthLifecycle",
    "GoogleOAuthProvider",
    "GoogleOAuthStateStore",
    "InMemoryGoogleCredentialStore",
    "InMemoryGoogleOAuthStateStore",
    "OAuthAuthorization",
    "OAuthCallbackResponse",
    "OAuthCredentialRecord",
    "OAuthExchangeError",
    "OAuthGrant",
    "SQLiteGoogleOAuthStateStore",
    "os",
]
