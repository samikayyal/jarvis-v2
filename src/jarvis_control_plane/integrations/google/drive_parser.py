"""Bounded Google Drive response parsing and text-content policy."""

from __future__ import annotations

from collections.abc import Mapping

from .http import GoogleHttpError, GoogleHttpResponse, ensure_bounded_response_body
from .read_models import GoogleReadProviderError

_TEXT_EXPORT_MIME_TYPES = frozenset({"text/plain", "text/csv", "text/markdown"})
_TEXT_MEDIA_MIME_TYPES = frozenset(
    {
        "text/plain",
        "text/csv",
        "text/markdown",
        "application/json",
        "application/xml",
        "text/xml",
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
    }
)


def response_mime_type(response: GoogleHttpResponse) -> str:
    """Read the case-insensitive media type from one Google response."""

    for name, value in response.headers.items():
        if name.lower() == "content-type":
            return value.split(";", 1)[0].strip().lower()
    return ""


def decode_text_export(response: GoogleHttpResponse) -> str:
    content_type = response_mime_type(response)
    if content_type not in _TEXT_EXPORT_MIME_TYPES:
        raise GoogleReadProviderError("unavailable", "Google export was not text")
    return decode_text_media(response, _TEXT_EXPORT_MIME_TYPES)


def decode_text_media(
    response: GoogleHttpResponse, approved_mime_types: frozenset[str]
) -> str:
    try:
        ensure_bounded_response_body(response.body)
    except GoogleHttpError as exc:
        raise GoogleReadProviderError(exc.code, str(exc)) from exc
    if response.status_code != 200:
        detail = response.body.decode("utf-8", errors="replace")
        code = "rate_limited" if response.status_code == 429 else "unavailable"
        raise GoogleReadProviderError(code, detail)
    content_type = response_mime_type(response)
    if content_type not in approved_mime_types:
        raise GoogleReadProviderError("unavailable", "Google export was not text")
    try:
        return response.body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GoogleReadProviderError(
            "unavailable", "Google export was not UTF-8 text"
        ) from error


def drive_file_with_media(
    metadata: Mapping[str, object],
    *,
    media_response: GoogleHttpResponse | None,
) -> dict[str, object]:
    """Add media only when metadata identifies an allowlisted text MIME type."""

    mime_type = metadata.get("mimeType")
    result: dict[str, object] = dict(metadata)
    if mime_type in _TEXT_MEDIA_MIME_TYPES:
        if media_response is None:
            raise GoogleReadProviderError(
                "unavailable", "Google text media response was missing"
            )
        result["content"] = decode_text_media(media_response, _TEXT_MEDIA_MIME_TYPES)
    else:
        result["content_unavailable"] = "unsupported_mime_type"
    return result


_response_mime_type = response_mime_type
