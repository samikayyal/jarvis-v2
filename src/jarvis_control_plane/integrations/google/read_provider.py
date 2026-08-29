"""Fixed Gmail and Drive provider edge with bounded HTTP responses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal, Protocol
from urllib.parse import quote, urlencode

from .drive_parser import (
    _TEXT_EXPORT_MIME_TYPES,
    _TEXT_MEDIA_MIME_TYPES,
    decode_text_export,
    decode_text_media,
    drive_file_with_media,
    response_mime_type,
)
from .gmail_parser import gmail_message_fields, parse_gmail_response
from .http import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    MAX_GOOGLE_HTTP_RESPONSE_BYTES,
    GoogleHttpError,
    GoogleHttpResponse,
    GoogleHttpTransport,
    ensure_bounded_response_body,
)
from .oauth_models import OAuthCredentialRecord
from .read_models import (
    GoogleReadOperation,
    GoogleReadProviderError,
    GoogleReadProviderResult,
    GoogleReadRequest,
)
from .token_auth import GoogleRefreshTokenExchanger, GoogleTokenExchangeError

MAX_PROVIDER_RESPONSE_BYTES = MAX_GOOGLE_HTTP_RESPONSE_BYTES

_GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
_DRIVE_API_ROOT = "https://www.googleapis.com/drive/v3"


class GoogleApiReadProvider:
    """Live, fixed-surface Google reader; it has no generic API operation."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: GoogleHttpTransport | None = None,
        timeout_seconds: float = GOOGLE_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._token_exchange = GoogleRefreshTokenExchanger(
            client_id=client_id,
            client_secret=client_secret,
            transport=transport,
            timeout_seconds=timeout_seconds,
        )
        self._transport = self._token_exchange.transport
        self._timeout_seconds = self._token_exchange.timeout_seconds

    def read(
        self, *, request: GoogleReadRequest, credential: OAuthCredentialRecord
    ) -> GoogleReadProviderResult:
        access_token = self._refresh_access_token(credential.refresh_token)
        response = self._authorized_get(request, access_token)
        if request.operation == "drive_files_export":
            return GoogleReadProviderResult(items=(self._decode_text_export(response),))
        payload = self._json_response(response)
        if request.operation == "drive_files_get":
            return self._drive_file_result(request, payload, access_token)
        return GoogleReadProviderResult(
            items=_response_items(request.operation, payload),
            continuation_token=_continuation_token(payload),
        )

    def _drive_file_result(
        self,
        request: GoogleReadRequest,
        metadata: Mapping[str, object],
        access_token: str,
    ) -> GoogleReadProviderResult:
        """Return metadata and media only for an allowlisted text file."""

        media = None
        if metadata.get("mimeType") in _TEXT_MEDIA_MIME_TYPES:
            media = self._authorized_drive_media_get(
                request.arguments["file_id"], access_token
            )
        result = drive_file_with_media(metadata, media_response=media)
        return GoogleReadProviderResult(
            items=(
                json.dumps(
                    result, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ),
            )
        )

    def _refresh_access_token(self, refresh_token: str) -> str:
        try:
            return self._token_exchange.exchange(refresh_token).access_token
        except GoogleTokenExchangeError as exc:
            raise GoogleReadProviderError(exc.code, str(exc)) from exc

    def _request(
        self,
        *,
        method: Literal["GET", "POST"],
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> GoogleHttpResponse:
        try:
            return self._transport.request(
                method=method,
                url=url,
                headers=dict(headers),
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except GoogleHttpError as exc:
            raise GoogleReadProviderError(exc.code, str(exc)) from exc

    def _authorized_get(
        self, request: GoogleReadRequest, access_token: str
    ) -> GoogleHttpResponse:
        return self._request(
            method="GET",
            url=_google_read_url(request),
            headers={"Authorization": f"Bearer {access_token}"},
            body=None,
        )

    def _authorized_drive_media_get(
        self, file_id: str, access_token: str
    ) -> GoogleHttpResponse:
        return self._request(
            method="GET",
            url=_drive_media_url(file_id),
            headers={"Authorization": f"Bearer {access_token}"},
            body=None,
        )

    def _json_response(self, response: GoogleHttpResponse) -> Mapping[str, object]:
        _ensure_response_size(response.body)
        if response.status_code != 200:
            _raise_http_failure(response)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GoogleReadProviderError(
                "unavailable", "Google returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise GoogleReadProviderError(
                "unavailable", "Google returned a non-object JSON body"
            )
        return payload

    def _decode_text_export(self, response: GoogleHttpResponse) -> str:
        _ensure_response_size(response.body)
        return decode_text_export(response)

    def _decode_text_media(
        self,
        response: GoogleHttpResponse,
        approved_mime_types: frozenset[str],
    ) -> str:
        _ensure_response_size(response.body)
        return decode_text_media(response, approved_mime_types)


class GoogleReadProvider(Protocol):
    """The connector-only provider edge; no generic HTTP surface is available."""

    def read(
        self, *, request: GoogleReadRequest, credential: OAuthCredentialRecord
    ) -> GoogleReadProviderResult: ...


class ControlledGoogleReadProvider:
    """Deterministic provider double used by contract and broker-seam tests."""

    def __init__(
        self,
        *,
        result: GoogleReadProviderResult | None = None,
        failure: str | None = None,
    ) -> None:
        self.result = result or GoogleReadProviderResult()
        self.failure = failure
        self.calls: list[tuple[str, dict[str, str], int]] = []

    def read(
        self, *, request: GoogleReadRequest, credential: OAuthCredentialRecord
    ) -> GoogleReadProviderResult:
        self.calls.append(
            (request.operation, dict(request.arguments), request.max_results)
        )
        if self.failure is not None:
            raise GoogleReadProviderError(self.failure)
        return self.result


def _google_read_url(request: GoogleReadRequest) -> str:
    operation = request.operation
    arguments = request.arguments
    if operation == "gmail_messages_list":
        return _url(
            f"{_GMAIL_API_ROOT}/messages",
            {
                "q": arguments["query"],
                "maxResults": request.max_results,
                "fields": "messages(id,threadId),nextPageToken,resultSizeEstimate",
            },
        )
    if operation == "gmail_messages_get":
        return _url(
            f"{_GMAIL_API_ROOT}/messages/{quote(arguments['message_id'], safe='')}",
            {
                "format": "full",
                "metadataHeaders": (
                    "From",
                    "To",
                    "Cc",
                    "Subject",
                    "Date",
                    "Message-ID",
                    "References",
                ),
                "fields": gmail_message_fields(),
            },
        )
    if operation == "gmail_threads_list":
        return _url(
            f"{_GMAIL_API_ROOT}/threads",
            {
                "q": arguments["query"],
                "maxResults": request.max_results,
                "fields": "threads(id,historyId,snippet),nextPageToken,resultSizeEstimate",
            },
        )
    if operation == "gmail_threads_get":
        return _url(
            f"{_GMAIL_API_ROOT}/threads/{quote(arguments['thread_id'], safe='')}",
            {
                "format": "full",
                "metadataHeaders": (
                    "From",
                    "To",
                    "Cc",
                    "Subject",
                    "Date",
                    "Message-ID",
                    "References",
                ),
                "fields": "id,historyId,messages(" + gmail_message_fields() + ")",
            },
        )
    if operation == "drive_files_list":
        return _url(
            f"{_DRIVE_API_ROOT}/files",
            {
                "q": arguments["query"],
                "pageSize": request.max_results,
                "fields": "files(id,name,mimeType,description,modifiedTime,size,webViewLink),nextPageToken",
            },
        )
    if operation == "drive_files_get":
        return _url(
            f"{_DRIVE_API_ROOT}/files/{quote(arguments['file_id'], safe='')}",
            {"fields": "id,name,mimeType,description,modifiedTime,size,webViewLink"},
        )
    if operation == "drive_files_export":
        mime_type = arguments["mime_type"]
        if mime_type not in _TEXT_EXPORT_MIME_TYPES:
            raise GoogleReadProviderError(
                "unavailable", "Drive export mime type was not allowed"
            )
        return _url(
            f"{_DRIVE_API_ROOT}/files/{quote(arguments['file_id'], safe='')}/export",
            {"mimeType": mime_type},
        )
    raise AssertionError(f"unsupported Google read operation: {operation}")


def _url(endpoint: str, query: Mapping[str, object]) -> str:
    flattened: list[tuple[str, str]] = []
    for key, value in query.items():
        if isinstance(value, tuple):
            flattened.extend((key, str(item)) for item in value)
        else:
            flattened.append((key, str(value)))
    return f"{endpoint}?{urlencode(flattened)}"


def _response_items(
    operation: GoogleReadOperation, payload: Mapping[str, object]
) -> tuple[str, ...]:
    collection_key = {
        "gmail_messages_list": "messages",
        "gmail_threads_list": "threads",
        "drive_files_list": "files",
    }.get(operation)
    if operation in {"gmail_messages_get", "gmail_threads_get"}:
        return parse_gmail_response(operation, payload)
    values: object = (
        payload if collection_key is None else payload.get(collection_key, ())
    )
    if not isinstance(values, (Mapping, list, tuple)):
        raise GoogleReadProviderError(
            "unavailable", "Google response had an invalid result shape"
        )
    rows = (values,) if isinstance(values, Mapping) else values
    if not all(isinstance(row, Mapping) for row in rows):
        raise GoogleReadProviderError(
            "unavailable", "Google response had an invalid item"
        )
    return tuple(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for row in rows
    )


def _continuation_token(payload: Mapping[str, object]) -> str | None:
    token = payload.get("nextPageToken")
    if token is None:
        return None
    if not isinstance(token, str) or not token:
        raise GoogleReadProviderError(
            "unavailable", "Google response had an invalid page token"
        )
    return token


def _ensure_response_size(body: bytes) -> None:
    try:
        ensure_bounded_response_body(body)
    except GoogleHttpError as exc:
        raise GoogleReadProviderError(exc.code, str(exc)) from exc


def _raise_http_failure(response: GoogleHttpResponse) -> None:
    detail = response.body.decode("utf-8", errors="replace")
    code = "rate_limited" if response.status_code == 429 else "unavailable"
    raise GoogleReadProviderError(code, detail)


def _drive_media_url(file_id: str) -> str:
    return _url(f"{_DRIVE_API_ROOT}/files/{quote(file_id, safe='')}", {"alt": "media"})


# These aliases keep the former provider vocabulary available to the facade.
_response_mime_type = response_mime_type
