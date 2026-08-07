"""Neutral, bounded HTTPS infrastructure shared by Google connectors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GOOGLE_HTTP_TIMEOUT_SECONDS = 5.0
MAX_GOOGLE_HTTP_RESPONSE_BYTES = 512 * 1024

GoogleHttpMethod = Literal["GET", "POST"]


class GoogleHttpError(RuntimeError):
    """Transport failure with a connector-neutral, bounded classification."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        super().__init__(detail or code)


@dataclass(frozen=True, slots=True)
class GoogleHttpResponse:
    """One response returned by the shared HTTP edge."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool):
            raise TypeError("Google HTTP status_code must be an integer")
        object.__setattr__(self, "headers", dict(self.headers))
        if not isinstance(self.body, bytes):
            raise TypeError("Google HTTP response body must be bytes")


class GoogleHttpTransport(Protocol):
    """The narrow HTTP capability used by every Google connector."""

    def request(
        self,
        *,
        method: GoogleHttpMethod,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> GoogleHttpResponse: ...


class UrllibGoogleHttpTransport:
    """Production HTTPS transport with a hard response-size cap."""

    def request(
        self,
        *,
        method: GoogleHttpMethod,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> GoogleHttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            response = urlopen(request, timeout=timeout_seconds)
        except HTTPError as error:
            try:
                return GoogleHttpResponse(
                    status_code=error.code,
                    headers=dict(error.headers.items()),
                    body=read_bounded_response_body(error),
                )
            finally:
                error.close()
        except TimeoutError as error:
            raise GoogleHttpError("timeout", str(error)) from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise GoogleHttpError("timeout", str(error)) from error
            raise GoogleHttpError("unavailable", str(error)) from error
        except OSError as error:
            raise GoogleHttpError("unavailable", str(error)) from error
        try:
            return GoogleHttpResponse(
                status_code=response.getcode(),
                headers=dict(response.headers.items()),
                body=read_bounded_response_body(response),
            )
        finally:
            response.close()


def read_bounded_response_body(response: object) -> bytes:
    """Read one provider body while rejecting the first byte beyond the cap."""

    body = response.read(MAX_GOOGLE_HTTP_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    if not isinstance(body, bytes):
        raise GoogleHttpError("unavailable", "Google returned a non-bytes body")
    ensure_bounded_response_body(body)
    return body


def ensure_bounded_response_body(body: bytes) -> None:
    """Enforce the common provider-response bound for injected transports too."""

    if not isinstance(body, bytes):
        raise TypeError("Google response body must be bytes")
    if len(body) > MAX_GOOGLE_HTTP_RESPONSE_BYTES:
        raise GoogleHttpError("oversized", "Google response exceeded the fixed limit")
