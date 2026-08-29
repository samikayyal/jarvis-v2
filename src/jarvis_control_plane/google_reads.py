"""Compatibility facade for the bounded Gmail and Drive read integration."""

# The private aliases retain compatibility with the former monolithic module.
# ruff: noqa: F401
from .google_auth import (
    GoogleRefreshTokenExchanger,
    GoogleTokenExchangeError,
    GoogleTokenExchangeRequest,
    GoogleTokenExchangeResult,
)
from .google_http import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    MAX_GOOGLE_HTTP_RESPONSE_BYTES,
    GoogleHttpError,
    GoogleHttpResponse,
    GoogleHttpTransport,
    UrllibGoogleHttpTransport,
    ensure_bounded_response_body,
)
from .google_oauth import (
    GoogleConnectionBinding,
    GoogleCredentialStore,
    GoogleOAuthLifecycle,
    OAuthCredentialRecord,
)
from .integrations.google.drive_parser import (
    _TEXT_EXPORT_MIME_TYPES,
    _TEXT_MEDIA_MIME_TYPES,
    _response_mime_type,
)
from .integrations.google.gmail_parser import (
    _decode_gmail_part,
    _gmail_full_fields,
    _gmail_headers,
    _gmail_message_fields,
    _gmail_message_with_text,
    _gmail_payload_text,
    _gmail_response_items,
    _html_to_text,
    _HtmlTextExtractor,
    _is_attachment_part,
)
from .integrations.google.read_connector import (
    GoogleReadConnector,
    build_live_google_read_connector,
)
from .integrations.google.read_models import (
    _OPERATION_SCOPES,
    _SERVICE_BY_OPERATION,
    DEFAULT_MAX_ITEM_BYTES,
    DEFAULT_MAX_RESULT_BYTES,
    DEFAULT_MAX_RESULT_ITEMS,
    DRIVE_READ_SCOPE,
    GMAIL_READ_SCOPE,
    GOOGLE_READ_SCOPES,
    GOOGLE_READ_TRACE_PAYLOAD_LIMIT_BYTES,
    MAX_ITEM_BYTES,
    MAX_RESULT_BYTES,
    MAX_RESULT_ITEMS,
    GoogleReadError,
    GoogleReadOperation,
    GoogleReadProviderError,
    GoogleReadProviderResult,
    GoogleReadRequest,
    GoogleReadResult,
    GoogleReadTracePayload,
)
from .integrations.google.read_policy import (
    _bounded_items,
    _content_available,
    _content_unavailable_reason,
    _limit,
    _non_blank,
    _requested_count,
    _safe_provider_failure,
    _serialized_result,
    _text,
)
from .integrations.google.read_provider import (
    MAX_PROVIDER_RESPONSE_BYTES,
    ControlledGoogleReadProvider,
    GoogleApiReadProvider,
    GoogleReadProvider,
    _continuation_token,
    _drive_media_url,
    _ensure_response_size,
    _google_read_url,
    _raise_http_failure,
    _response_items,
    _url,
)
from .integrations.google.read_tools import (
    DriveReadInput,
    GmailReadInput,
    GoogleReadOutput,
    _google_read_tools,
    _output,
)

# orchestration.py historically recognizes this compatibility module when it
# maps a connector failure to a safe operator-facing reason.
GoogleReadError.__module__ = __name__

GoogleReadHttpResponse = GoogleHttpResponse
GoogleReadHttpTransport = GoogleHttpTransport
UrllibGoogleReadHttpTransport = UrllibGoogleHttpTransport

__all__ = [
    "DEFAULT_MAX_ITEM_BYTES",
    "DEFAULT_MAX_RESULT_BYTES",
    "DEFAULT_MAX_RESULT_ITEMS",
    "DRIVE_READ_SCOPE",
    "GMAIL_READ_SCOPE",
    "GOOGLE_READ_SCOPES",
    "GOOGLE_READ_TRACE_PAYLOAD_LIMIT_BYTES",
    "MAX_ITEM_BYTES",
    "MAX_PROVIDER_RESPONSE_BYTES",
    "MAX_RESULT_BYTES",
    "MAX_RESULT_ITEMS",
    "ControlledGoogleReadProvider",
    "DriveReadInput",
    "GmailReadInput",
    "GoogleApiReadProvider",
    "GoogleReadConnector",
    "GoogleReadError",
    "GoogleReadHttpResponse",
    "GoogleReadHttpTransport",
    "GoogleReadOperation",
    "GoogleReadOutput",
    "GoogleReadProvider",
    "GoogleReadProviderError",
    "GoogleReadProviderResult",
    "GoogleReadRequest",
    "GoogleReadResult",
    "GoogleReadTracePayload",
    "GoogleRefreshTokenExchanger",
    "GoogleTokenExchangeError",
    "GoogleTokenExchangeRequest",
    "GoogleTokenExchangeResult",
    "UrllibGoogleReadHttpTransport",
    "build_live_google_read_connector",
]
