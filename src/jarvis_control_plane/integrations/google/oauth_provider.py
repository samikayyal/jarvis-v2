"""Fixed-surface Google OAuth provider implementations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .oauth_models import (
    GoogleOAuthError,
    OAuthAuthorization,
    OAuthExchangeError,
    OAuthGrant,
    _canonical_string,
)


class GoogleOAuthProvider(Protocol):
    """The narrow live-provider edge; no generic Google methods are exposed here."""

    def exchange_code(
        self, *, code: str, requested_scopes: frozenset[str]
    ) -> OAuthGrant: ...

    def revoke(self, *, refresh_token: str) -> None: ...


class _RejectOAuthRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class GoogleLiveOAuthProvider:
    """Fixed-endpoint authorization-code exchange and token revocation edge."""

    _TOKEN_URL = "https://oauth2.googleapis.com/token"
    _USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
    _REVOKE_URL = "https://oauth2.googleapis.com/revoke"

    def __init__(
        self, *, client_id: str, client_secret: str, redirect_uri: str
    ) -> None:
        self._client_id = _canonical_string(client_id, "client_id")
        self._client_secret = _canonical_string(client_secret, "client_secret")
        self._redirect_uri = _canonical_string(redirect_uri, "redirect_uri")
        self._opener = build_opener(_RejectOAuthRedirects())

    def authorization_url(self, authorization: OAuthAuthorization) -> str:
        """Build the exact operator-visible URL for one issued state."""

        if not isinstance(authorization, OAuthAuthorization):
            raise TypeError("authorization must be an OAuthAuthorization")
        if "openid" not in authorization.requested_scopes:
            raise GoogleOAuthError("live Google authorization requires openid")
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "access_type": "offline",
                "client_id": self._client_id,
                "include_granted_scopes": "false",
                "prompt": "consent",
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": " ".join(sorted(authorization.requested_scopes)),
                "state": authorization.state,
            }
        )

    def exchange_code(
        self, *, code: str, requested_scopes: frozenset[str]
    ) -> OAuthGrant:
        _canonical_string(code, "code")
        token = self._json_request(
            Request(
                self._TOKEN_URL,
                data=urlencode(
                    {
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": self._redirect_uri,
                    }
                ).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
        )
        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token")
        scope = token.get("scope")
        if not all(
            isinstance(value, str) and value
            for value in (access_token, refresh_token, scope)
        ):
            raise OAuthExchangeError("invalid_provider_response")
        granted_scopes = frozenset(scope.split())
        if not requested_scopes <= granted_scopes:
            raise OAuthExchangeError("missing_scope")
        user = self._json_request(
            Request(
                self._USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                method="GET",
            )
        )
        subject = user.get("sub")
        if not isinstance(subject, str) or not subject:
            raise OAuthExchangeError("invalid_provider_response")
        return OAuthGrant(
            subject=subject,
            granted_scopes=granted_scopes,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    def revoke(self, *, refresh_token: str) -> None:
        _canonical_string(refresh_token, "refresh_token")
        request = Request(
            self._REVOKE_URL,
            data=urlencode({"token": refresh_token}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                if response.getcode() != 200:
                    raise GoogleOAuthError("provider_failure")
                if len(response.read(65_537)) > 65_536:
                    raise GoogleOAuthError("provider_failure")
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise GoogleOAuthError("provider_failure") from exc

    def _json_request(self, request: Request) -> Mapping[str, object]:
        try:
            with self._opener.open(request, timeout=20) as response:
                if response.getcode() != 200:
                    raise OAuthExchangeError("provider_failure")
                body = response.read(65_537)
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise OAuthExchangeError("provider_failure") from exc
        if len(body) > 65_536:
            raise OAuthExchangeError("invalid_provider_response")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OAuthExchangeError("invalid_provider_response") from exc
        if not isinstance(payload, dict):
            raise OAuthExchangeError("invalid_provider_response")
        return payload


class ControlledGoogleOAuthProvider:
    """Controlled OAuth double that never contacts Google."""

    def __init__(
        self,
        *,
        grant: OAuthGrant,
        exchange_failure: str | None = None,
        revoke_failure: str | None = None,
    ) -> None:
        if exchange_failure is not None:
            OAuthExchangeError(exchange_failure)
        self.grant = grant
        self.exchange_failure = exchange_failure
        self.revoke_failure = revoke_failure
        self.exchange_calls: list[tuple[str, frozenset[str]]] = []
        self.revoke_calls: list[str] = []

    def exchange_code(
        self, *, code: str, requested_scopes: frozenset[str]
    ) -> OAuthGrant:
        _canonical_string(code, "code")
        self.exchange_calls.append((code, requested_scopes))
        if self.exchange_failure is not None:
            raise OAuthExchangeError(self.exchange_failure)
        return self.grant

    def revoke(self, *, refresh_token: str) -> None:
        _canonical_string(refresh_token, "refresh_token")
        self.revoke_calls.append(refresh_token)
        if self.revoke_failure is not None:
            raise GoogleOAuthError("provider_failure")
