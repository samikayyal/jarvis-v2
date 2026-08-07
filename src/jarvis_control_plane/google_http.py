"""Shared bounded Google HTTPS and OAuth access-token primitives."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_HTTP_TIMEOUT_SECONDS = 5.0
MAX_GOOGLE_TOKEN_RESPONSE_BYTES = 64 * 1024


class GoogleHttpError(OSError):
    """A bounded Google transport failure with a stable private code."""

    def __init__(self, code: Literal["timeout", "unavailable"], detail: str) -> None:
        self.code = code
        super().__init__(detail or code)


@dataclass(frozen=True, slots=True)
class GoogleHttpResponse:
    """One bounded response from the shared Google HTTP edge."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


class GoogleHttpTransport(Protocol):
    """The minimal transport shared by Google reads and Calendar writes."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> GoogleHttpResponse: ...


class UrllibGoogleHttpTransport:
    """HTTPS transport with one configurable response-size boundary."""

    def __init__(self, *, max_response_bytes: int) -> None:
        if not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._max_response_bytes = max_response_bytes

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> GoogleHttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            response = urlopen(request, timeout=timeout_seconds)
        except HTTPError as error:
            return GoogleHttpResponse(
                status_code=error.code,
                headers=dict(error.headers.items()),
                body=self._read_bounded_body(error),
            )
        except TimeoutError as error:
            raise GoogleHttpError("timeout", str(error)) from error
        except URLError as error:
            code: Literal["timeout", "unavailable"] = (
                "timeout" if isinstance(error.reason, TimeoutError) else "unavailable"
            )
            raise GoogleHttpError(code, str(error)) from error
        except OSError as error:
            raise GoogleHttpError("unavailable", str(error)) from error
        try:
            return GoogleHttpResponse(
                status_code=response.getcode(),
                headers=dict(response.headers.items()),
                body=self._read_bounded_body(response),
            )
        finally:
            response.close()

    def _read_bounded_body(self, response: object) -> bytes:
        body = response.read(self._max_response_bytes + 1)  # type: ignore[attr-defined]
        if not isinstance(body, bytes) or len(body) > self._max_response_bytes:
            raise GoogleHttpError(
                "unavailable", "Google response exceeded the fixed limit"
            )
        return body


class GoogleTokenRefreshError(RuntimeError):
    """A private access-token refresh failure classified by the shared helper."""

    def __init__(
        self, code: Literal["invalid_grant", "unavailable"], detail: str
    ) -> None:
        self.code = code
        super().__init__(detail or code)


class GoogleAccessTokenRefresher:
    """One implementation of Google refresh-token request and response policy."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: GoogleHttpTransport,
        timeout_seconds: float = GOOGLE_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        if (
            not isinstance(client_id, str)
            or not client_id
            or client_id.strip() != client_id
        ):
            raise ValueError("client_id must be a non-empty canonical string")
        if (
            not isinstance(client_secret, str)
            or not client_secret
            or client_secret.strip() != client_secret
        ):
            raise ValueError("client_secret must be a non-empty canonical string")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < timeout_seconds <= GOOGLE_HTTP_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be positive and no greater than 5")
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def refresh(self, refresh_token: str) -> str:
        body = urlencode(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode("ascii")
        try:
            response = self._transport.request(
                method="POST",
                url=GOOGLE_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except GoogleHttpError as exc:
            raise GoogleTokenRefreshError("unavailable", str(exc)) from exc
        if (
            not isinstance(response.body, bytes)
            or len(response.body) > MAX_GOOGLE_TOKEN_RESPONSE_BYTES
        ):
            raise GoogleTokenRefreshError(
                "unavailable", "token response exceeded the fixed limit"
            )
        if response.status_code != 200:
            detail = response.body.decode("utf-8", errors="replace")
            try:
                payload = json.loads(detail)
            except json.JSONDecodeError:
                payload = None
            code: Literal["invalid_grant", "unavailable"] = (
                "invalid_grant"
                if isinstance(payload, dict) and payload.get("error") == "invalid_grant"
                else "unavailable"
            )
            raise GoogleTokenRefreshError(code, detail)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleTokenRefreshError(
                "unavailable", "token response was not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise GoogleTokenRefreshError(
                "unavailable", "token response was not an object"
            )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise GoogleTokenRefreshError(
                "unavailable", "token response lacked access_token"
            )
        return token
