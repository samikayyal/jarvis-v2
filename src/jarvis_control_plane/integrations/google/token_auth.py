"""Shared Google refresh-token exchange used by read and write connectors."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from .http import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    GoogleHttpError,
    GoogleHttpResponse,
    GoogleHttpTransport,
    UrllibGoogleHttpTransport,
    ensure_bounded_response_body,
)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True, slots=True)
class GoogleTokenExchangeRequest:
    """The exact refresh-token request, retained for connector diagnostics."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    form: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GoogleTokenExchangeResult:
    """The access token plus the complete exchange evidence."""

    access_token: str
    request: GoogleTokenExchangeRequest
    response: GoogleHttpResponse


class GoogleTokenExchangeError(RuntimeError):
    """Refresh-token failure with the exchange evidence available to callers."""

    def __init__(
        self,
        code: str,
        *,
        request: GoogleTokenExchangeRequest,
        response: GoogleHttpResponse | None = None,
        detail: str | None = None,
    ) -> None:
        self.code = code
        self.request = request
        self.response = response
        super().__init__(detail or code)


class GoogleRefreshTokenExchanger:
    """One bounded implementation of Google's OAuth refresh-token protocol."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: GoogleHttpTransport | None = None,
        timeout_seconds: float = GOOGLE_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._client_id = _canonical_string(client_id, "client_id")
        self._client_secret = _canonical_string(client_secret, "client_secret")
        self._transport = transport or UrllibGoogleHttpTransport()
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= GOOGLE_HTTP_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be positive and no greater than 5")
        self._timeout_seconds = float(timeout_seconds)

    @property
    def transport(self) -> GoogleHttpTransport:
        return self._transport

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def exchange(self, refresh_token: str) -> GoogleTokenExchangeResult:
        form = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": _canonical_string(refresh_token, "refresh_token"),
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        request = GoogleTokenExchangeRequest(
            method="POST",
            url=GOOGLE_TOKEN_URL,
            headers=headers,
            body=urlencode(form).encode("ascii"),
            form=form,
        )
        try:
            response = self._transport.request(
                method="POST",
                url=request.url,
                headers=dict(request.headers),
                body=request.body,
                timeout_seconds=self._timeout_seconds,
            )
        except GoogleHttpError as exc:
            raise GoogleTokenExchangeError(
                exc.code, request=request, detail=str(exc)
            ) from exc
        except Exception as exc:
            raise GoogleTokenExchangeError(
                "unavailable", request=request, detail=str(exc)
            ) from exc

        try:
            ensure_bounded_response_body(response.body)
        except GoogleHttpError as exc:
            raise GoogleTokenExchangeError(
                exc.code, request=request, response=response, detail=str(exc)
            ) from exc

        if response.status_code != 200:
            code = _http_failure_code(response)
            raise GoogleTokenExchangeError(
                code,
                request=request,
                response=response,
                detail=response.body.decode("utf-8", errors="replace"),
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleTokenExchangeError(
                "invalid_response", request=request, response=response
            ) from exc
        if not isinstance(payload, dict):
            raise GoogleTokenExchangeError(
                "invalid_response", request=request, response=response
            )
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise GoogleTokenExchangeError(
                "invalid_response", request=request, response=response
            )
        return GoogleTokenExchangeResult(
            access_token=access_token,
            request=request,
            response=response,
        )


def _canonical_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-blank canonical string")
    return value


def _http_failure_code(response: GoogleHttpResponse) -> str:
    if response.status_code == 429:
        return "rate_limited"
    if response.status_code == 400:
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("error") == "invalid_grant":
            return "invalid_grant"
    return "unavailable"
