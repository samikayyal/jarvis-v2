"""Compatibility facade for the shared Google HTTP transport."""

from .integrations.google.http import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    MAX_GOOGLE_HTTP_RESPONSE_BYTES,
    GoogleHttpError,
    GoogleHttpMethod,
    GoogleHttpResponse,
    GoogleHttpTransport,
    UrllibGoogleHttpTransport,
    ensure_bounded_response_body,
    read_bounded_response_body,
)

__all__ = [
    "GOOGLE_HTTP_TIMEOUT_SECONDS",
    "MAX_GOOGLE_HTTP_RESPONSE_BYTES",
    "GoogleHttpError",
    "GoogleHttpMethod",
    "GoogleHttpResponse",
    "GoogleHttpTransport",
    "UrllibGoogleHttpTransport",
    "ensure_bounded_response_body",
    "read_bounded_response_body",
]
