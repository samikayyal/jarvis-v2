"""Compatibility facade for Google refresh-token authentication."""

from .application.compatibility import install_mirrors
from .integrations.google import token_auth as _token_auth
from .integrations.google.http import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    MAX_GOOGLE_HTTP_RESPONSE_BYTES,
    GoogleHttpError,
    GoogleHttpResponse,
    GoogleHttpTransport,
    UrllibGoogleHttpTransport,
    ensure_bounded_response_body,
)
from .integrations.google.token_auth import (
    GOOGLE_TOKEN_URL,
    GoogleRefreshTokenExchanger,
    GoogleTokenExchangeError,
    GoogleTokenExchangeRequest,
    GoogleTokenExchangeResult,
)

_canonical_string = _token_auth._canonical_string
_http_failure_code = _token_auth._http_failure_code

__all__ = [
    "GOOGLE_HTTP_TIMEOUT_SECONDS",
    "GOOGLE_TOKEN_URL",
    "MAX_GOOGLE_HTTP_RESPONSE_BYTES",
    "GoogleHttpError",
    "GoogleHttpResponse",
    "GoogleHttpTransport",
    "GoogleRefreshTokenExchanger",
    "GoogleTokenExchangeError",
    "GoogleTokenExchangeRequest",
    "GoogleTokenExchangeResult",
    "UrllibGoogleHttpTransport",
    "ensure_bounded_response_body",
]

install_mirrors(
    __name__,
    {
        "_canonical_string": (_token_auth,),
        "_http_failure_code": (_token_auth,),
    },
)
