"""Configuration and bounded HTTP value objects for the OpenWA adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urlsplit

OPENWA_HTTP_TIMEOUT_SECONDS = 5.0
MAX_OPENWA_HTTP_RESPONSE_BYTES = 64 * 1024
OPENWA_MESSAGE_MAX_CHARACTERS = 4096

OpenWAHttpMethod = Literal["GET", "POST"]


def _canonical_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty canonical string")
    return value


@dataclass(frozen=True, slots=True)
class OpenWAConfig:
    """Fixed routing plus the one injected OpenWA operator credential."""

    api_base_url: str
    api_key: str = field(repr=False)
    internal_session_id: str
    named_session: str
    operator_conversation_id: str
    max_text_characters: int = OPENWA_MESSAGE_MAX_CHARACTERS

    def __post_init__(self) -> None:
        for name in (
            "api_base_url",
            "api_key",
            "internal_session_id",
            "named_session",
            "operator_conversation_id",
        ):
            _canonical_text(getattr(self, name), name)
        parsed = urlsplit(self.api_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("api_base_url must be a canonical HTTP(S) URL")
        object.__setattr__(self, "api_base_url", self.api_base_url.rstrip("/"))
        if self.max_text_characters != OPENWA_MESSAGE_MAX_CHARACTERS:
            raise ValueError("OpenWA message length is fixed at 4,096 characters")

    @property
    def authorization_headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    def api_url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("OpenWA API paths must be absolute")
        return f"{self.api_base_url}{path}"


class OpenWAHttpError(RuntimeError):
    """Bounded transport failure with explicit external-side-effect uncertainty."""

    def __init__(self, code: str, *, may_have_sent: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.may_have_sent = may_have_sent


@dataclass(frozen=True, slots=True)
class OpenWAHttpResponse:
    status_code: int
    body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool):
            raise TypeError("OpenWA HTTP status_code must be an integer")
        if not isinstance(self.body, bytes):
            raise TypeError("OpenWA HTTP response body must be bytes")
        if len(self.body) > MAX_OPENWA_HTTP_RESPONSE_BYTES:
            raise ValueError("OpenWA HTTP response exceeded the fixed limit")


class OpenWAHttpTransport(Protocol):
    def request(
        self,
        *,
        method: OpenWAHttpMethod,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> OpenWAHttpResponse: ...


@dataclass(frozen=True, slots=True)
class OpenWAHttpRequest:
    method: OpenWAHttpMethod
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout_seconds: float


__all__ = [
    "MAX_OPENWA_HTTP_RESPONSE_BYTES",
    "OPENWA_HTTP_TIMEOUT_SECONDS",
    "OPENWA_MESSAGE_MAX_CHARACTERS",
    "OpenWAConfig",
    "OpenWAHttpError",
    "OpenWAHttpMethod",
    "OpenWAHttpRequest",
    "OpenWAHttpResponse",
    "OpenWAHttpTransport",
]
