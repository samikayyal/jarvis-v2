"""Connector-owned Google credential storage and state/credential binding."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .oauth_models import (
    GoogleConnectionState,
    GoogleOAuthError,
    OAuthCredentialRecord,
    _canonical_string,
)
from .oauth_state import GoogleOAuthStateStore


class GoogleCredentialStore(Protocol):
    """Private connector-owned persistence for the sole refresh-token record."""

    @property
    def current(self) -> OAuthCredentialRecord | None: ...

    def replace(self, credential: OAuthCredentialRecord) -> None: ...

    def delete(self) -> None: ...


@dataclass(frozen=True, slots=True)
class GoogleConnectionSnapshot:
    """One lock-consistent view of connection state and its credential."""

    connection: GoogleConnectionState
    credential: OAuthCredentialRecord | None


class GoogleConnectionBinding:
    """Shared synchronization boundary for OAuth state and credential changes."""

    def __init__(
        self,
        *,
        state_store: GoogleOAuthStateStore,
        credential_store: GoogleCredentialStore,
    ) -> None:
        self._state_store = state_store
        self._credential_store = credential_store
        self.synchronization_lock = state_store.synchronization_lock

    def snapshot(self) -> GoogleConnectionSnapshot:
        """Read generation and credential while no lifecycle mutation can interleave."""

        with self.synchronization_lock:
            return GoogleConnectionSnapshot(
                connection=self._state_store.get_connection(),
                credential=self._credential_store.current,
            )


class InMemoryGoogleCredentialStore:
    """Controlled credential-store double; it is never an ordinary state store."""

    def __init__(self, current: OAuthCredentialRecord | None = None) -> None:
        self._current = current

    @property
    def current(self) -> OAuthCredentialRecord | None:
        return self._current

    def replace(self, credential: OAuthCredentialRecord) -> None:
        self._current = credential

    def delete(self) -> None:
        self._current = None


class FileGoogleCredentialStore:
    """One 0600 credential file replaced atomically within a private directory."""

    def __init__(
        self, directory: str | Path, *, filename: str = "google-oauth.json"
    ) -> None:
        self._directory = Path(directory)
        self._filename = _canonical_string(filename, "filename")
        if Path(self._filename).name != self._filename:
            raise ValueError("credential filename must not contain a path")
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._restrict_directory()

    @property
    def _path(self) -> Path:
        return self._directory / self._filename

    @property
    def _metadata_path(self) -> Path:
        return self._directory / f"{self._filename}.meta"

    def _restrict_directory(self) -> None:
        if os.name != "nt":
            os.chmod(self._directory, 0o700)

    @property
    def current(self) -> OAuthCredentialRecord | None:
        if not self._path.exists():
            return None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            fields = set(payload)
            if fields == {"granted_scopes", "refresh_token", "subject"}:
                connection_generation = self._legacy_generation(payload)
            elif fields == {
                "connection_generation",
                "granted_scopes",
                "refresh_token",
                "subject",
            }:
                credential = OAuthCredentialRecord(
                    subject=payload["subject"],
                    granted_scopes=frozenset(payload["granted_scopes"]),
                    refresh_token=payload["refresh_token"],
                    connection_generation=payload["connection_generation"],
                )
                self._write_legacy_compatible_record(credential)
                return credential
            else:
                raise ValueError("credential record has an unexpected shape")
            return OAuthCredentialRecord(
                subject=payload["subject"],
                granted_scopes=frozenset(payload["granted_scopes"]),
                refresh_token=payload["refresh_token"],
                connection_generation=connection_generation,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise GoogleOAuthError("credential_store_unavailable") from exc

    def replace(self, credential: OAuthCredentialRecord) -> None:
        self._write_legacy_compatible_record(credential)

    def _write_legacy_compatible_record(
        self, credential: OAuthCredentialRecord
    ) -> None:
        previous = self._read_existing_record()
        records = []
        if previous is not None and previous.refresh_token != credential.refresh_token:
            records.append(self._metadata_record(previous))
        records.append(self._metadata_record(credential))
        metadata = {
            "records": records,
            "schema": "google_oauth_credential_metadata_v2",
        }
        payload = {
            "granted_scopes": sorted(credential.granted_scopes),
            "refresh_token": credential.refresh_token,
            "subject": credential.subject,
        }
        self._atomic_write(self._metadata_path, metadata)
        self._atomic_write(self._path, payload)

    def _read_existing_record(self) -> OAuthCredentialRecord | None:
        if not self._path.exists():
            return None
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        fields = set(payload)
        if fields == {"granted_scopes", "refresh_token", "subject"}:
            connection_generation = self._legacy_generation(payload)
        elif fields == {
            "connection_generation",
            "granted_scopes",
            "refresh_token",
            "subject",
        }:
            connection_generation = payload["connection_generation"]
        else:
            raise ValueError("credential record has an unexpected shape")
        return OAuthCredentialRecord(
            subject=payload["subject"],
            granted_scopes=frozenset(payload["granted_scopes"]),
            refresh_token=payload["refresh_token"],
            connection_generation=connection_generation,
        )

    @staticmethod
    def _metadata_record(credential: OAuthCredentialRecord) -> dict[str, object]:
        return {
            "connection_generation": credential.connection_generation,
            "refresh_token_sha256": hashlib.sha256(
                credential.refresh_token.encode("utf-8")
            ).hexdigest(),
        }

    def _legacy_generation(self, payload: Mapping[str, object]) -> int:
        if not self._metadata_path.exists():
            return 0
        metadata = json.loads(self._metadata_path.read_text(encoding="utf-8"))
        refresh_token = payload.get("refresh_token")
        if not isinstance(refresh_token, str):
            raise TypeError("credential record has an invalid refresh token")
        fingerprint = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        if (
            isinstance(metadata, Mapping)
            and set(metadata)
            == {"connection_generation", "refresh_token_sha256", "schema"}
            and metadata["schema"] == "google_oauth_credential_metadata_v1"
        ):
            if metadata["refresh_token_sha256"] != fingerprint:
                return 0
            return int(metadata["connection_generation"])
        if (
            not isinstance(metadata, Mapping)
            or set(metadata) != {"records", "schema"}
            or metadata["schema"] != "google_oauth_credential_metadata_v2"
            or not isinstance(metadata["records"], list)
            or not metadata["records"]
        ):
            raise ValueError("credential metadata has an unexpected shape")
        matches = [
            record
            for record in metadata["records"]
            if isinstance(record, Mapping)
            and set(record) == {"connection_generation", "refresh_token_sha256"}
            and record["refresh_token_sha256"] == fingerprint
        ]
        if not matches:
            return 0
        generations = {record["connection_generation"] for record in matches}
        if len(generations) != 1:
            raise ValueError("credential metadata has conflicting generations")
        generation = next(iter(generations))
        if not isinstance(generation, int):
            raise TypeError("credential metadata has an invalid generation")
        return generation

    def _atomic_write(self, path: Path, payload: Mapping[str, object]) -> None:
        temporary = self._directory / f".{path.name}.{secrets.token_hex(16)}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                os.chmod(path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def delete(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
            self._metadata_path.unlink(missing_ok=True)
        except OSError as exc:
            raise GoogleOAuthError("credential_store_unavailable") from exc
