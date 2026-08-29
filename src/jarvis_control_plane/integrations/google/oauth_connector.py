"""Connector-owned OAuth exchange, revocation, and credential replacement."""

from __future__ import annotations

from dataclasses import dataclass

from .credentials import GoogleCredentialStore
from .oauth_models import (
    OAuthCredentialRecord,
    OAuthExchangeError,
    OAuthGrant,
    _canonical_string,
)
from .oauth_provider import GoogleOAuthProvider


@dataclass(frozen=True, slots=True)
class _ExchangeReceipt:
    """Complete connector result retained only in the diagnostic trace boundary."""

    grant: OAuthGrant


@dataclass(frozen=True, slots=True)
class _RevocationReceipt:
    """Complete revocation input and result retained only in diagnostic traces."""

    credential: OAuthCredentialRecord | None
    provider_result: None = None


class GoogleOAuthConnector:
    """Exchange code and atomically own refresh-token replacement in one boundary."""

    def __init__(
        self,
        *,
        configured_identity: str,
        provider: GoogleOAuthProvider,
        credential_store: GoogleCredentialStore,
    ) -> None:
        self._configured_identity = _canonical_string(
            configured_identity, "configured_identity"
        )
        self._provider = provider
        self._credential_store = credential_store

    def exchange_and_replace(
        self,
        *,
        code: str,
        requested_scopes: frozenset[str],
        connection_generation: int = 0,
    ) -> _ExchangeReceipt:
        grant = self._provider.exchange_code(
            code=code, requested_scopes=requested_scopes
        )
        if grant.subject != self._configured_identity:
            raise OAuthExchangeError("wrong_identity")
        if not requested_scopes <= grant.granted_scopes:
            raise OAuthExchangeError("missing_scope")
        if not grant.refresh_token:
            raise OAuthExchangeError("missing_refresh_token")
        self._credential_store.replace(
            OAuthCredentialRecord(
                subject=grant.subject,
                granted_scopes=grant.granted_scopes,
                refresh_token=grant.refresh_token,
                connection_generation=connection_generation,
            )
        )
        return _ExchangeReceipt(grant=grant)

    @property
    def current_credential(self) -> OAuthCredentialRecord | None:
        """Expose the current credential only to the isolated trace call site."""

        return self._credential_store.current

    def disconnect(self) -> _RevocationReceipt:
        credential = self._credential_store.current
        if credential is None:
            return _RevocationReceipt(credential=None)
        try:
            self._provider.revoke(refresh_token=credential.refresh_token)
        finally:
            self._credential_store.delete()
        return _RevocationReceipt(credential=credential)

    def discard_local_credential(self) -> None:
        self._credential_store.delete()
