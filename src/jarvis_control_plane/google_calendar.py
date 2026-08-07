"""Exact, approval-gated Google Calendar create and change boundary.

The capability broker retains the approval and once-only dispatch transaction.
This module owns only the Calendar-specific half: producing a closed frozen
proposal and refusing to call a provider when its Calendar state is stale.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .google_oauth import (
    GoogleCredentialStore,
    GoogleOAuthError,
    GoogleOAuthStateStore,
    OAuthCredentialRecord,
)
from .models import FrozenActionProposal
from .ports import (
    ActionDispatcherError,
    DiagnosticTraceError,
    TraceCapacityError,
    TraceWriteError,
)
from .traces import DiagnosticTraceRecorder

CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CalendarWriteOperation = Literal["insert", "update", "patch"]
CalendarNotification = Literal["none", "all", "externalOnly"]
_WRITE_KINDS = frozenset({"calendar_insert", "calendar_update", "calendar_patch"})
_ARRAY_FIELDS = frozenset({"attendees", "recurrence", "reminders"})
GOOGLE_CALENDAR_TRACE_PAYLOAD_LIMIT_BYTES = 32 * 1024
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_CALENDAR_API_ROOT = "https://www.googleapis.com/calendar/v3"
_MAX_PROVIDER_RESPONSE_BYTES = 64 * 1024
_GOOGLE_HTTP_TIMEOUT_SECONDS = 5.0


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty canonical string")
    return value


def _canonical_json(value: object, name: str) -> object:
    try:
        frozen = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        canonical = json.loads(frozen)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} must be JSON-serializable") from exc
    return canonical


def _event(value: object) -> dict[str, object]:
    event = _canonical_json(value, "complete_event")
    if not isinstance(event, dict):
        raise TypeError("complete_event must be an object")
    for endpoint in ("start", "end"):
        value = event.get(endpoint)
        if not isinstance(value, dict) or not any(
            isinstance(value.get(field), str) and value[field].strip()
            for field in ("date", "dateTime")
        ):
            raise ValueError(f"complete_event requires a {endpoint} date or dateTime")
    for field in _ARRAY_FIELDS:
        if field in event and not isinstance(event[field], (list, dict)):
            raise ValueError(f"complete_event {field} has an invalid shape")
    return event


def _patch(value: object, complete_event: Mapping[str, object]) -> dict[str, object]:
    patch = _canonical_json(value, "reviewed_patch")
    if not isinstance(patch, dict) or not patch:
        raise ValueError("reviewed_patch must be a non-empty object")
    if {"id", "etag"} & set(patch):
        raise ValueError("reviewed_patch cannot replace event identity or ETag")
    for field in _ARRAY_FIELDS & set(patch):
        if field not in complete_event or patch[field] != complete_event[field]:
            raise ValueError(
                f"reviewed_patch {field} must equal the complete event value"
            )
    return patch


@dataclass(frozen=True, slots=True)
class CalendarWriteRequest:
    """One closed Calendar provider request reconstructed from a frozen proposal."""

    operation: CalendarWriteOperation
    calendar_id: str
    event_id: str | None
    complete_event: dict[str, object]
    etag: str | None
    notification: CalendarNotification
    connection_generation: int
    reviewed_patch: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.operation not in {"insert", "update", "patch"}:
            raise ValueError("Calendar operation is not allowed")
        _text(self.calendar_id, "calendar_id")
        _event(self.complete_event)
        if (
            not isinstance(self.connection_generation, int)
            or self.connection_generation < 0
        ):
            raise ValueError("connection_generation must be a non-negative integer")
        if self.notification not in {"none", "all", "externalOnly"}:
            raise ValueError("Calendar notification is not allowed")
        if self.operation == "insert":
            if (
                self.event_id is not None
                or self.etag is not None
                or self.reviewed_patch is not None
            ):
                raise ValueError("Calendar insert cannot carry existing-event fields")
        else:
            _text(self.event_id, "event_id")
            _text(self.etag, "etag")
            if self.operation == "patch":
                if self.reviewed_patch is None:
                    raise ValueError("Calendar patch requires a reviewed patch")
                _patch(self.reviewed_patch, self.complete_event)
            elif self.reviewed_patch is not None:
                raise ValueError("Calendar update cannot carry a reviewed patch")

    @classmethod
    def from_proposal(cls, proposal: FrozenActionProposal) -> CalendarWriteRequest:
        if proposal.kind not in _WRITE_KINDS:
            raise ValueError("proposal is not a Calendar write")
        try:
            payload = json.loads(proposal.payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Calendar proposal payload is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "calendar_id",
            "complete_event",
            "connection_generation",
            "etag",
            "event_id",
            "notification",
            "operation",
            "reviewed_patch",
            "schema",
        }:
            raise ValueError("Calendar proposal payload has an unexpected shape")
        if payload["schema"] != "calendar_write_v1":
            raise ValueError("Calendar proposal has an unsupported schema")
        operation = payload["operation"]
        expected_kind = f"calendar_{operation}"
        if proposal.kind != expected_kind:
            raise ValueError("Calendar proposal kind does not match its operation")
        return cls(
            operation=operation,
            calendar_id=payload["calendar_id"],
            event_id=payload["event_id"],
            complete_event=payload["complete_event"],
            etag=payload["etag"],
            notification=payload["notification"],
            connection_generation=payload["connection_generation"],
            reviewed_patch=payload["reviewed_patch"],
        )


class CalendarWriteProposal:
    """Creates the exact Calendar proposal stored by the generic approval flow."""

    @classmethod
    def insert(
        cls,
        *,
        action_id: str,
        request_id: str,
        calendar_id: str,
        complete_event: Mapping[str, object],
        notification: CalendarNotification,
        connection_generation: int,
    ) -> FrozenActionProposal:
        return cls._create(
            action_id=action_id,
            request_id=request_id,
            operation="insert",
            calendar_id=calendar_id,
            event_id=None,
            complete_event=complete_event,
            etag=None,
            notification=notification,
            connection_generation=connection_generation,
            reviewed_patch=None,
        )

    @classmethod
    def update(
        cls,
        *,
        action_id: str,
        request_id: str,
        calendar_id: str,
        event_id: str,
        complete_event: Mapping[str, object],
        etag: str,
        notification: CalendarNotification,
        connection_generation: int,
    ) -> FrozenActionProposal:
        return cls._create(
            action_id=action_id,
            request_id=request_id,
            operation="update",
            calendar_id=calendar_id,
            event_id=event_id,
            complete_event=complete_event,
            etag=etag,
            notification=notification,
            connection_generation=connection_generation,
            reviewed_patch=None,
        )

    @classmethod
    def patch(
        cls,
        *,
        action_id: str,
        request_id: str,
        calendar_id: str,
        event_id: str,
        complete_event: Mapping[str, object],
        reviewed_patch: Mapping[str, object],
        etag: str,
        notification: CalendarNotification,
        connection_generation: int,
    ) -> FrozenActionProposal:
        return cls._create(
            action_id=action_id,
            request_id=request_id,
            operation="patch",
            calendar_id=calendar_id,
            event_id=event_id,
            complete_event=complete_event,
            etag=etag,
            notification=notification,
            connection_generation=connection_generation,
            reviewed_patch=reviewed_patch,
        )

    @staticmethod
    def _create(
        *,
        action_id: str,
        request_id: str,
        operation: CalendarWriteOperation,
        calendar_id: str,
        event_id: str | None,
        complete_event: Mapping[str, object],
        etag: str | None,
        notification: CalendarNotification,
        connection_generation: int,
        reviewed_patch: Mapping[str, object] | None,
    ) -> FrozenActionProposal:
        request = CalendarWriteRequest(
            operation=operation,
            calendar_id=calendar_id,
            event_id=event_id,
            complete_event=_event(complete_event),
            etag=etag,
            notification=notification,
            connection_generation=connection_generation,
            reviewed_patch=(
                _patch(reviewed_patch, _event(complete_event))
                if reviewed_patch is not None
                else None
            ),
        )
        payload = {
            "schema": "calendar_write_v1",
            "operation": request.operation,
            "calendar_id": request.calendar_id,
            "event_id": request.event_id,
            "complete_event": request.complete_event,
            "etag": request.etag,
            "notification": request.notification,
            "connection_generation": request.connection_generation,
            "reviewed_patch": request.reviewed_patch,
        }
        preview = "Calendar change (exact JSON):\n" + json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True
        )
        return FrozenActionProposal.create(
            action_id=action_id,
            request_id=request_id,
            kind=f"calendar_{operation}",
            preview=preview,
            payload=payload,
        )


class GoogleCalendarWriteProviderError(RuntimeError):
    """A Calendar edge result that distinguishes known and ambiguous outcomes."""

    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        may_have_dispatched: bool = False,
    ) -> None:
        self.code = code
        self.may_have_dispatched = may_have_dispatched
        super().__init__(detail or code)


@dataclass(frozen=True, slots=True)
class GoogleCalendarWriteProviderResult:
    """The returned Calendar event retained by the diagnostic trace boundary."""

    event: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event", _event(self.event))


@dataclass(frozen=True, slots=True)
class GoogleCalendarHttpResponse:
    """Bounded response returned by the concrete Calendar HTTP edge."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


class GoogleCalendarHttpTransport(Protocol):
    """The only HTTP surface exposed to the Calendar write provider."""

    def request(
        self,
        *,
        method: Literal["POST", "PUT", "PATCH"],
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> GoogleCalendarHttpResponse: ...


class UrllibGoogleCalendarHttpTransport:
    """Production HTTPS transport; the provider owns outcome classification."""

    def request(
        self,
        *,
        method: Literal["POST", "PUT", "PATCH"],
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> GoogleCalendarHttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            response = urlopen(request, timeout=timeout_seconds)
        except HTTPError as error:
            return GoogleCalendarHttpResponse(
                status_code=error.code,
                headers=dict(error.headers.items()),
                body=_read_bounded_body(error),
            )
        try:
            return GoogleCalendarHttpResponse(
                status_code=response.getcode(),
                headers=dict(response.headers.items()),
                body=_read_bounded_body(response),
            )
        finally:
            response.close()


class GoogleApiCalendarWriteProvider:
    """Concrete fixed-surface provider for Calendar insert, update, and patch."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: GoogleCalendarHttpTransport | None = None,
        timeout_seconds: float = _GOOGLE_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._client_id = _text(client_id, "client_id")
        self._client_secret = _text(client_secret, "client_secret")
        self._transport = transport or UrllibGoogleCalendarHttpTransport()
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < timeout_seconds <= _GOOGLE_HTTP_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be positive and no greater than 5")
        self._timeout_seconds = float(timeout_seconds)

    def write(
        self, *, request: CalendarWriteRequest, credential: OAuthCredentialRecord
    ) -> GoogleCalendarWriteProviderResult:
        access_token = self._refresh_access_token(credential.refresh_token)
        try:
            response = self._authorized_write(request, access_token)
        except (TimeoutError, URLError, OSError) as exc:
            raise GoogleCalendarWriteProviderError(
                "transport_failure", str(exc), may_have_dispatched=True
            ) from exc
        payload = self._write_response(response)
        self._verify_returned_event(request, payload)
        return GoogleCalendarWriteProviderResult(event=payload)

    def _refresh_access_token(self, refresh_token: str) -> str:
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
                url=_GOOGLE_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except (TimeoutError, URLError, OSError) as exc:
            raise GoogleCalendarWriteProviderError(
                "token_unavailable", str(exc)
            ) from exc
        payload = self._token_response(response)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise GoogleCalendarWriteProviderError(
                "token_unavailable", "token response lacked access_token"
            )
        return token

    def _authorized_write(
        self, request: CalendarWriteRequest, access_token: str
    ) -> GoogleCalendarHttpResponse:
        method: Literal["POST", "PUT", "PATCH"] = {
            "insert": "POST",
            "update": "PUT",
            "patch": "PATCH",
        }[request.operation]
        event_path = (
            "" if request.event_id is None else "/" + quote(request.event_id, safe="")
        )
        url = (
            f"{_CALENDAR_API_ROOT}/calendars/"
            f"{quote(request.calendar_id, safe='')}/events{event_path}?"
            f"{urlencode({'sendUpdates': request.notification})}"
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        if request.etag is not None:
            headers["If-Match"] = request.etag
        body_payload = (
            request.reviewed_patch
            if request.operation == "patch"
            else request.complete_event
        )
        return self._transport.request(
            method=method,
            url=url,
            headers=headers,
            body=json.dumps(
                body_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8"),
            timeout_seconds=self._timeout_seconds,
        )

    @staticmethod
    def _token_response(response: GoogleCalendarHttpResponse) -> Mapping[str, object]:
        if response.status_code != 200:
            detail = response.body.decode("utf-8", errors="replace")
            try:
                payload = json.loads(detail)
            except json.JSONDecodeError:
                payload = None
            code = (
                "invalid_grant"
                if isinstance(payload, dict) and payload.get("error") == "invalid_grant"
                else "token_unavailable"
            )
            raise GoogleCalendarWriteProviderError(code, detail)
        return _json_object(response.body, "token response")

    @staticmethod
    def _write_response(response: GoogleCalendarHttpResponse) -> Mapping[str, object]:
        if response.status_code not in {200, 201}:
            detail = response.body.decode("utf-8", errors="replace")
            if response.status_code == 412:
                raise GoogleCalendarWriteProviderError("concurrent_change", detail)
            if 400 <= response.status_code < 500:
                raise GoogleCalendarWriteProviderError("rejected", detail)
            raise GoogleCalendarWriteProviderError(
                "server_unavailable", detail, may_have_dispatched=True
            )
        try:
            return _json_object(response.body, "Calendar write response")
        except GoogleCalendarWriteProviderError as exc:
            raise GoogleCalendarWriteProviderError(
                exc.code, str(exc), may_have_dispatched=True
            ) from exc

    @staticmethod
    def _verify_returned_event(
        request: CalendarWriteRequest, returned: Mapping[str, object]
    ) -> None:
        if request.event_id is not None and returned.get("id") != request.event_id:
            raise GoogleCalendarWriteProviderError(
                "returned_event_mismatch",
                "Calendar returned a different event identity",
                may_have_dispatched=True,
            )
        if not _contains_complete(request.complete_event, returned):
            raise GoogleCalendarWriteProviderError(
                "returned_event_mismatch",
                "Calendar returned content different from the approved event",
                may_have_dispatched=True,
            )


class GoogleCalendarWriteProvider(Protocol):
    """Fixed create/update/patch provider edge; delete and calendar mutation are absent."""

    def write(
        self, *, request: CalendarWriteRequest, credential: OAuthCredentialRecord
    ) -> GoogleCalendarWriteProviderResult: ...


class ControlledGoogleCalendarWriteProvider:
    """Controlled Calendar edge for approval and failure-contract tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[CalendarWriteRequest, OAuthCredentialRecord]] = []
        self.failure: tuple[str, bool] | None = None

    def write(
        self, *, request: CalendarWriteRequest, credential: OAuthCredentialRecord
    ) -> GoogleCalendarWriteProviderResult:
        self.calls.append((request, credential))
        if self.failure is not None:
            message, may_have_dispatched = self.failure
            raise GoogleCalendarWriteProviderError(
                "controlled_failure", message, may_have_dispatched=may_have_dispatched
            )
        event = dict(request.complete_event)
        if request.event_id is not None:
            event["id"] = request.event_id
        return GoogleCalendarWriteProviderResult(event=event)


class CalendarActionDispatcher:
    """Dispatches only a current, frozen Calendar write through the closed provider."""

    def __init__(
        self,
        *,
        configured_identity: str,
        connection_state: GoogleOAuthStateStore,
        credential_store: GoogleCredentialStore,
        provider: GoogleCalendarWriteProvider,
        trace: DiagnosticTraceRecorder,
        on_invalid_grant: Callable[[], object] | None = None,
    ) -> None:
        self._configured_identity = _text(configured_identity, "configured_identity")
        self._connection_state = connection_state
        self._credential_store = credential_store
        self._provider = provider
        if not isinstance(trace, DiagnosticTraceRecorder):
            raise TypeError("trace must be a DiagnosticTraceRecorder")
        self._trace = trace
        self._on_invalid_grant = on_invalid_grant

    def dispatch(self, action: FrozenActionProposal) -> None:
        try:
            request = CalendarWriteRequest.from_proposal(action)
        except (TypeError, ValueError) as exc:
            raise ActionDispatcherError("Calendar proposal is invalid") from exc
        provider_started = False

        def invoke_provider() -> GoogleCalendarWriteProviderResult:
            nonlocal provider_started
            provider_started = True
            return self._provider.write(request=request, credential=credential)

        try:
            with self._connection_state.dispatch_lease():
                connection = self._connection_state.get_connection()
                credential = self._credential_store.current
                if (
                    not connection.connected
                    or connection.generation != request.connection_generation
                    or CALENDAR_WRITE_SCOPE not in connection.granted_scopes
                ):
                    raise ActionDispatcherError("Calendar proposal is stale")
                if (
                    credential is None
                    or credential.subject != self._configured_identity
                    or CALENDAR_WRITE_SCOPE not in credential.granted_scopes
                    or credential.connection_generation != connection.generation
                ):
                    raise ActionDispatcherError("Calendar connection is unavailable")
                self._trace.execute(
                    request_id=action.request_id,
                    operation_id=f"{action.action_id}:calendar:{request.operation}",
                    operation_type="google_calendar_write",
                    input_payload=request,
                    arguments={
                        "action_id": action.action_id,
                        "operation": request.operation,
                        "calendar_id": request.calendar_id,
                        "event_id": request.event_id,
                        "connection_generation": request.connection_generation,
                    },
                    telemetry={"service": "calendar", "operation": request.operation},
                    operation=invoke_provider,
                    result_limit_bytes=GOOGLE_CALENDAR_TRACE_PAYLOAD_LIMIT_BYTES,
                    error_limit_bytes=GOOGLE_CALENDAR_TRACE_PAYLOAD_LIMIT_BYTES,
                )
        except GoogleCalendarWriteProviderError as exc:
            if exc.code == "invalid_grant" and self._on_invalid_grant is not None:
                try:
                    self._on_invalid_grant()
                except (GoogleOAuthError, OSError) as cleanup_error:
                    raise ActionDispatcherError(
                        "Calendar credential invalidation failed"
                    ) from cleanup_error
            raise ActionDispatcherError(
                str(exc), may_have_dispatched=exc.may_have_dispatched
            ) from exc
        except TraceCapacityError as exc:
            raise ActionDispatcherError(
                "Calendar trace admission is unavailable"
            ) from exc
        except TraceWriteError as exc:
            raise ActionDispatcherError(
                "Calendar trace retention failed",
                may_have_dispatched=exc.operation_started,
            ) from exc
        except DiagnosticTraceError as exc:
            raise ActionDispatcherError(
                "Calendar trace admission is unavailable"
            ) from exc
        except ActionDispatcherError:
            raise
        except Exception as exc:
            raise ActionDispatcherError(
                "Calendar provider is unavailable", may_have_dispatched=provider_started
            ) from exc


def build_live_calendar_action_dispatcher(
    *,
    configured_identity: str,
    connection_state: GoogleOAuthStateStore,
    credential_store: GoogleCredentialStore,
    on_invalid_grant: Callable[[], object],
    client_id: str,
    client_secret: str,
    trace: DiagnosticTraceRecorder,
    transport: GoogleCalendarHttpTransport | None = None,
) -> CalendarActionDispatcher:
    """Compose the production Calendar capability from fixed, injectable edges."""

    return CalendarActionDispatcher(
        configured_identity=configured_identity,
        connection_state=connection_state,
        credential_store=credential_store,
        provider=GoogleApiCalendarWriteProvider(
            client_id=client_id,
            client_secret=client_secret,
            transport=transport,
        ),
        trace=trace,
        on_invalid_grant=on_invalid_grant,
    )


def _read_bounded_body(response: object) -> bytes:
    body = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    if not isinstance(body, bytes) or len(body) > _MAX_PROVIDER_RESPONSE_BYTES:
        raise OSError("Calendar response exceeded the fixed limit")
    return body


def _json_object(body: bytes, name: str) -> Mapping[str, object]:
    if len(body) > _MAX_PROVIDER_RESPONSE_BYTES:
        raise GoogleCalendarWriteProviderError(
            "invalid_response", f"{name} was too large"
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoogleCalendarWriteProviderError(
            "invalid_response", f"{name} was not JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise GoogleCalendarWriteProviderError(
            "invalid_response", f"{name} was not an object"
        )
    return payload


def _contains_complete(expected: object, actual: object) -> bool:
    """Accept provider-added fields while proving every frozen event field survived."""

    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _contains_complete(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                _contains_complete(left, right) for left, right in zip(expected, actual)
            )
        )
    return expected == actual
