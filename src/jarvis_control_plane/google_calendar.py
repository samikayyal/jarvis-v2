"""Exact, approval-gated Google Calendar create and change boundary.

The capability broker retains the approval and once-only dispatch transaction.
This module owns only the Calendar-specific half: producing a closed frozen
proposal and refusing to call a provider when its Calendar state is stale.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Literal, Protocol
from urllib.parse import quote, urlencode

from .google_auth import GoogleRefreshTokenExchanger, GoogleTokenExchangeError
from .google_http import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    GoogleHttpResponse,
    GoogleHttpTransport,
    UrllibGoogleHttpTransport,
)
from .google_oauth import (
    GoogleCredentialStore,
    GoogleOAuthError,
    GoogleOAuthStateStore,
    OAuthCredentialRecord,
)
from .models import FrozenActionProposal
from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ActionDispatchHandle,
    AuditWriteError,
    DiagnosticTraceError,
    TraceCapacityError,
    TraceWriteError,
)
from .traces import DiagnosticTraceRecorder

CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CalendarWriteOperation = Literal["insert", "update", "patch"]
CalendarNotification = Literal["none", "all", "externalOnly"]
_WRITE_KINDS = frozenset({"calendar_insert", "calendar_update", "calendar_patch"})
_REQUIRED_COMPLETE_EVENT_FIELDS = frozenset(
    {"attendees", "recurrence", "reminders", "visibility"}
)
_VISIBILITY_VALUES = frozenset({"default", "public", "private", "confidential"})
_STATUS_VALUES = frozenset({"confirmed", "tentative", "cancelled"})
_EVENT_TYPE_VALUES = frozenset(
    {
        "birthday",
        "default",
        "focusTime",
        "fromGmail",
        "outOfOffice",
        "workingLocation",
    }
)
_INSERT_EVENT_TYPE_VALUES = _EVENT_TYPE_VALUES - {"fromGmail"}
_EVENT_IDENTITY_FIELDS = frozenset({"id", "etag"})
# A complete Event GET also returns server-managed fields.  They are removed
# from mutation snapshots explicitly so a full PUT preserves writable state
# without sending identity or read-only metadata back to Google.
_READ_ONLY_EVENT_FIELDS = frozenset(
    {
        "kind",
        "htmlLink",
        "created",
        "updated",
        "creator",
        "organizer",
        "recurringEventId",
        "originalStartTime",
        "iCalUID",
        "hangoutLink",
        "locked",
        "privateCopy",
    }
)
GOOGLE_CALENDAR_TRACE_PAYLOAD_LIMIT_BYTES = 32 * 1024
_CALENDAR_API_ROOT = "https://www.googleapis.com/calendar/v3"
_MAX_PROVIDER_RESPONSE_BYTES = 64 * 1024
_GOOGLE_HTTP_TIMEOUT_SECONDS = GOOGLE_HTTP_TIMEOUT_SECONDS
_UNORDERED_COLLECTION_PATHS = frozenset(
    {
        ("attendees",),
        ("attachments",),
        ("conferenceData", "entryPoints"),
        ("recurrence",),
        ("reminders", "overrides"),
    }
)
_CALENDAR_EVENT_DEFAULTS: dict[str, object] = {
    "anyoneCanAddSelf": False,
    "attendeesOmitted": False,
    "endTimeUnspecified": False,
    "guestsCanInviteOthers": True,
    "guestsCanModify": False,
    "guestsCanSeeOtherGuests": True,
    "status": "confirmed",
    "transparency": "opaque",
    "visibility": "default",
}
_CALENDAR_ATTENDEE_DEFAULTS: dict[str, object] = {
    "additionalGuests": 0,
    "optional": False,
    "resource": False,
}


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


def _validate_event_structure(
    event: dict[str, object], operation: CalendarWriteOperation
) -> None:
    if "etag" in event or ("id" in event and operation != "insert"):
        identity_fields = _EVENT_IDENTITY_FIELDS & set(event)
        raise ValueError(
            "complete_event cannot replace event identity or ETag: "
            + ", ".join(sorted(identity_fields))
        )
    if "id" in event:
        _text(event["id"], "complete_event id")

    rejected_fields = _READ_ONLY_EVENT_FIELDS & set(event)
    if rejected_fields:
        raise ValueError(
            "complete_event contains read-only fields: "
            + ", ".join(sorted(rejected_fields))
        )

    if "eventType" in event and (
        not isinstance(event["eventType"], str)
        or event["eventType"] not in _EVENT_TYPE_VALUES
    ):
        raise ValueError("complete_event eventType has an invalid value")
    if operation == "insert" and event.get("eventType") not in {
        None,
        *_INSERT_EVENT_TYPE_VALUES,
    }:
        raise ValueError("complete_event eventType cannot be created")


def _validate_event_status(event: Mapping[str, object]) -> None:
    status = event.get("status")
    if status is not None and (
        not isinstance(status, str) or status not in _STATUS_VALUES
    ):
        raise ValueError("complete_event status has an invalid value")
    if status == "cancelled":
        raise ValueError("complete_event cannot represent a cancelled/deleted event")


def _validate_event_material_fields(event: Mapping[str, object]) -> None:
    if "attendeesOmitted" in event:
        if not isinstance(event["attendeesOmitted"], bool):
            raise TypeError("complete_event attendeesOmitted has an invalid shape")
        if event["attendeesOmitted"]:
            raise ValueError(
                "complete_event cannot rely on an event with omitted attendees"
            )
    missing = _REQUIRED_COMPLETE_EVENT_FIELDS - set(event)
    if missing:
        raise ValueError(
            "complete_event requires explicit material fields: "
            + ", ".join(sorted(missing))
        )
    _validate_event_endpoints(event)
    attendees = event["attendees"]
    if not isinstance(attendees, list):
        raise TypeError("complete_event attendees has an invalid shape")
    recurrence = event["recurrence"]
    if not isinstance(recurrence, list):
        raise TypeError("complete_event recurrence has an invalid shape")
    visibility = event["visibility"]
    if not isinstance(visibility, str) or visibility not in _VISIBILITY_VALUES:
        raise ValueError("complete_event visibility has an invalid value")
    _validate_event_reminders(event["reminders"])


def _validate_event_endpoints(event: Mapping[str, object]) -> None:
    for endpoint in ("start", "end"):
        value = event.get(endpoint)
        if (
            not isinstance(value, dict)
            or sum(
                isinstance(value.get(field), str) and bool(value[field].strip())
                for field in ("date", "dateTime")
            )
            != 1
        ):
            raise ValueError(f"complete_event requires a {endpoint} date or dateTime")


def _validate_event_reminders(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"overrides", "useDefault"}:
        raise ValueError(
            "complete_event reminders must explicitly contain useDefault and overrides"
        )
    if not isinstance(value["useDefault"], bool) or not isinstance(
        value["overrides"], list
    ):
        raise TypeError("complete_event reminders has an invalid shape")


def _event(
    value: object, *, operation: CalendarWriteOperation = "update"
) -> dict[str, object]:
    event = _canonical_json(value, "complete_event")
    if not isinstance(event, dict):
        raise TypeError("complete_event must be an object")
    _validate_event_structure(event, operation)
    _validate_event_status(event)
    _validate_event_material_fields(event)
    return event


def _apply_calendar_semantic_defaults(event: Mapping[str, object]) -> dict[str, object]:
    """Apply documented Calendar defaults before freezing or comparing events."""

    normalized = dict(event)
    normalized.setdefault("attendees", [])
    normalized.setdefault("recurrence", [])
    normalized.setdefault("reminders", {"useDefault": True, "overrides": []})
    for field, default in _CALENDAR_EVENT_DEFAULTS.items():
        normalized.setdefault(field, default)

    attendees = normalized.get("attendees")
    if isinstance(attendees, list):
        normalized["attendees"] = [
            {
                **_CALENDAR_ATTENDEE_DEFAULTS,
                **dict(attendee),
            }
            if isinstance(attendee, Mapping)
            else attendee
            for attendee in attendees
        ]

    reminders = normalized.get("reminders")
    if isinstance(reminders, Mapping) and "overrides" not in reminders:
        normalized["reminders"] = {**reminders, "overrides": []}
    return normalized


def _snapshot_event(
    value: object, *, operation: CalendarWriteOperation = "update"
) -> dict[str, object]:
    """Create a complete writable snapshot from Google's event representation."""

    raw_event = _canonical_json(value, "current_event")
    if not isinstance(raw_event, dict):
        raise TypeError("current_event must be an object")
    identity = {
        field: raw_event[field]
        for field in _EVENT_IDENTITY_FIELDS
        if field in raw_event
    }
    event = {
        field: field_value
        for field, field_value in raw_event.items()
        if field not in _READ_ONLY_EVENT_FIELDS and field not in _EVENT_IDENTITY_FIELDS
    }
    if event.get("status") == "cancelled":
        raise ValueError("current_event cannot represent a cancelled/deleted event")
    normalized = _event(
        _apply_calendar_semantic_defaults(event),
        operation=operation,
    )
    normalized.update(identity)
    return normalized


def _patch(value: object, complete_event: Mapping[str, object]) -> dict[str, object]:
    patch = _canonical_json(value, "reviewed_patch")
    if not isinstance(patch, dict) or not patch:
        raise ValueError("reviewed_patch must be a non-empty object")
    if _EVENT_IDENTITY_FIELDS & set(patch):
        raise ValueError("reviewed_patch cannot replace event identity or ETag")
    read_only_fields = (_READ_ONLY_EVENT_FIELDS | {"eventType"}) & set(patch)
    if read_only_fields:
        raise ValueError(
            "reviewed_patch contains read-only fields: "
            + ", ".join(sorted(read_only_fields))
        )
    if patch.get("status") == "cancelled":
        raise ValueError("reviewed_patch cannot represent a cancelled/deleted event")
    for field in patch:
        if field not in complete_event or patch[field] != complete_event[field]:
            raise ValueError(
                f"reviewed_patch {field} must equal the complete event value"
            )
    return patch


@dataclass(frozen=True, slots=True)
class CalendarEventSnapshot:
    """An ETag-bound, already complete event fetched before a change proposal."""

    event: dict[str, object]
    etag: str

    def __post_init__(self) -> None:
        normalized = _snapshot_event(self.event)
        _text(self.etag, "etag")
        if "etag" in normalized and normalized["etag"] != self.etag:
            raise ValueError("Calendar event snapshot ETag does not match its event")
        object.__setattr__(self, "event", normalized)

    def resulting_event(self, changes: Mapping[str, object]) -> dict[str, object]:
        normalized_changes = _canonical_json(changes, "event_changes")
        if not isinstance(normalized_changes, dict):
            raise TypeError("event_changes must be an object")
        if _EVENT_IDENTITY_FIELDS & set(normalized_changes):
            raise ValueError("event_changes cannot replace event identity or ETag")
        if "eventType" in normalized_changes and normalized_changes[
            "eventType"
        ] != self.event.get("eventType"):
            raise ValueError("eventType cannot be changed after event creation")
        result = {
            key: value for key, value in self.event.items() if key not in {"id", "etag"}
        }
        result.update(normalized_changes)
        return _event(result, operation="update")

    def require_event_id(self, event_id: str) -> None:
        _text(event_id, "event_id")
        source_event_id = self.event.get("id")
        if source_event_id is not None and source_event_id != event_id:
            raise ValueError("Calendar event snapshot identity does not match event_id")


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
        normalized_event = _event(self.complete_event, operation=self.operation)
        object.__setattr__(self, "complete_event", normalized_event)
        if (
            not isinstance(self.connection_generation, int)
            or self.connection_generation < 0
        ):
            raise ValueError("connection_generation must be a non-negative integer")
        if self.notification not in {"none", "all", "externalOnly"}:
            raise ValueError("Calendar notification is not allowed")
        if self.operation == "insert":
            if self.etag is not None or self.reviewed_patch is not None:
                raise ValueError("Calendar insert cannot carry existing-event fields")
            event_identity = normalized_event.get("id")
            if self.event_id is not None:
                _text(self.event_id, "event_id")
                if event_identity is None:
                    normalized_event["id"] = self.event_id
                    event_identity = self.event_id
                elif event_identity != self.event_id:
                    raise ValueError(
                        "Calendar insert event identity does not match complete_event"
                    )
            if event_identity is not None:
                _text(event_identity, "complete_event id")
                object.__setattr__(self, "event_id", event_identity)
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
        event_id: str | None = None,
    ) -> FrozenActionProposal:
        return cls._create(
            action_id=action_id,
            request_id=request_id,
            operation="insert",
            calendar_id=calendar_id,
            event_id=event_id,
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
        snapshot: CalendarEventSnapshot,
        changes: Mapping[str, object],
        notification: CalendarNotification,
        connection_generation: int,
    ) -> FrozenActionProposal:
        if not isinstance(snapshot, CalendarEventSnapshot):
            raise TypeError("Calendar update requires an ETag-bound event snapshot")
        snapshot.require_event_id(event_id)
        return cls._create(
            action_id=action_id,
            request_id=request_id,
            operation="update",
            calendar_id=calendar_id,
            event_id=event_id,
            complete_event=snapshot.resulting_event(changes),
            etag=snapshot.etag,
            notification=notification,
            connection_generation=connection_generation,
            reviewed_patch=None,
        )

    @classmethod
    def update_from_snapshot(
        cls,
        *,
        action_id: str,
        request_id: str,
        calendar_id: str,
        event_id: str,
        snapshot: CalendarEventSnapshot,
        changes: Mapping[str, object],
        notification: CalendarNotification,
        connection_generation: int,
    ) -> FrozenActionProposal:
        """Derive a complete PUT body from the fetched ETag-bound event."""

        if not isinstance(snapshot, CalendarEventSnapshot):
            raise TypeError("Calendar update requires an ETag-bound event snapshot")
        return cls.update(
            action_id=action_id,
            request_id=request_id,
            calendar_id=calendar_id,
            event_id=event_id,
            snapshot=snapshot,
            changes=changes,
            notification=notification,
            connection_generation=connection_generation,
        )

    @classmethod
    def patch(
        cls,
        *,
        action_id: str,
        request_id: str,
        calendar_id: str,
        event_id: str,
        snapshot: CalendarEventSnapshot,
        reviewed_patch: Mapping[str, object],
        notification: CalendarNotification,
        connection_generation: int,
    ) -> FrozenActionProposal:
        if not isinstance(snapshot, CalendarEventSnapshot):
            raise TypeError("Calendar patch requires an ETag-bound event snapshot")
        snapshot.require_event_id(event_id)
        return cls._create(
            action_id=action_id,
            request_id=request_id,
            operation="patch",
            calendar_id=calendar_id,
            event_id=event_id,
            complete_event=snapshot.resulting_event(reviewed_patch),
            etag=snapshot.etag,
            notification=notification,
            connection_generation=connection_generation,
            reviewed_patch=reviewed_patch,
        )

    @classmethod
    def patch_from_snapshot(
        cls,
        *,
        action_id: str,
        request_id: str,
        calendar_id: str,
        event_id: str,
        snapshot: CalendarEventSnapshot,
        reviewed_patch: Mapping[str, object],
        notification: CalendarNotification,
        connection_generation: int,
    ) -> FrozenActionProposal:
        """Freeze a reviewed PATCH against the exact fetched event snapshot."""

        if not isinstance(snapshot, CalendarEventSnapshot):
            raise TypeError("Calendar patch requires an ETag-bound event snapshot")
        return cls.patch(
            action_id=action_id,
            request_id=request_id,
            calendar_id=calendar_id,
            event_id=event_id,
            snapshot=snapshot,
            reviewed_patch=reviewed_patch,
            notification=notification,
            connection_generation=connection_generation,
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
        normalized_event = _event(complete_event, operation=operation)
        if operation == "insert" and event_id is None:
            candidate_event_id = normalized_event.get("id")
            if candidate_event_id is not None:
                event_id = _text(candidate_event_id, "complete_event id")
        if (
            operation == "insert"
            and event_id is not None
            and "id" not in normalized_event
        ):
            normalized_event["id"] = event_id
        normalized_patch = (
            _patch(reviewed_patch, normalized_event)
            if reviewed_patch is not None
            else None
        )
        request = CalendarWriteRequest(
            operation=operation,
            calendar_id=calendar_id,
            event_id=event_id,
            complete_event=normalized_event,
            etag=etag,
            notification=notification,
            connection_generation=connection_generation,
            reviewed_patch=normalized_patch,
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
        normalized = _canonical_json(self.event, "provider_event")
        if not isinstance(normalized, dict):
            raise TypeError("provider_event must be an object")
        object.__setattr__(self, "event", normalized)


GoogleCalendarHttpResponse = GoogleHttpResponse
GoogleCalendarHttpTransport = GoogleHttpTransport


class UrllibGoogleCalendarHttpTransport(UrllibGoogleHttpTransport):
    """Calendar view of the shared bounded Google HTTPS transport."""


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
        self._token_exchange = GoogleRefreshTokenExchanger(
            client_id=client_id,
            client_secret=client_secret,
            transport=transport,
            timeout_seconds=timeout_seconds,
        )
        self._transport = self._token_exchange.transport
        self._timeout_seconds = self._token_exchange.timeout_seconds

    def write(
        self, *, request: CalendarWriteRequest, credential: OAuthCredentialRecord
    ) -> GoogleCalendarWriteProviderResult:
        access_token = self._refresh_access_token(credential.refresh_token)
        try:
            response = self._authorized_write(request, access_token)
        except OSError as exc:
            raise GoogleCalendarWriteProviderError(
                "transport_failure", str(exc), may_have_dispatched=True
            ) from exc
        payload = self._write_response(response)
        self._verify_returned_event(request, payload)
        return GoogleCalendarWriteProviderResult(event=payload)

    def _refresh_access_token(self, refresh_token: str) -> str:
        try:
            return self._token_exchange.exchange(refresh_token).access_token
        except GoogleTokenExchangeError as exc:
            code = (
                "invalid_grant" if exc.code == "invalid_grant" else "token_unavailable"
            )
            raise GoogleCalendarWriteProviderError(code, str(exc)) from exc

    def _authorized_write(
        self, request: CalendarWriteRequest, access_token: str
    ) -> GoogleCalendarHttpResponse:
        method: Literal["POST", "PUT", "PATCH"] = {
            "insert": "POST",
            "update": "PUT",
            "patch": "PATCH",
        }[request.operation]
        event_path = (
            ""
            if request.operation == "insert"
            else "/" + quote(request.event_id, safe="")
        )
        body_payload = (
            request.reviewed_patch
            if request.operation == "patch"
            else request.complete_event
        )
        query: dict[str, str] = {"sendUpdates": request.notification}
        if "conferenceData" in body_payload:
            query["conferenceDataVersion"] = "1"
        if "attachments" in body_payload:
            query["supportsAttachments"] = "true"
        if "eventLabelId" in body_payload:
            query["eventLabelVersion"] = "1"
        url = (
            f"{_CALENDAR_API_ROOT}/calendars/"
            f"{quote(request.calendar_id, safe='')}/events{event_path}?"
            f"{urlencode(query)}"
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        if request.etag is not None:
            headers["If-Match"] = request.etag
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
        try:
            expected = _snapshot_event(
                request.complete_event, operation=request.operation
            )
            actual = _snapshot_event(returned, operation=request.operation)
        except (TypeError, ValueError) as exc:
            raise GoogleCalendarWriteProviderError(
                "returned_event_mismatch",
                "Calendar returned an invalid event representation",
                may_have_dispatched=True,
            ) from exc
        if not _contains_complete(expected, actual):
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


@dataclass(slots=True)
class _CalendarDispatchContext:
    provider_started: bool = False


class _CalendarWriteDispatch:
    """Prepared Calendar write cancellable until its provider attempt starts."""

    def __init__(
        self, owner: CalendarActionDispatcher, action: FrozenActionProposal
    ) -> None:
        self._owner = owner
        self._action = action
        self._lock = RLock()
        self._started = False
        self._cancelled = False

    def run(self) -> object | None:
        with self._lock:
            if self._cancelled:
                self._owner._forget(self._action.action_id, self)
                raise ActionDispatcherError(
                    "Calendar action was cancelled before dispatch"
                )
            self._started = True
        try:
            self._owner.dispatch(self._action)
        finally:
            self._owner._forget(self._action.action_id, self)

    def cancel(self) -> ActionCancellationResult:
        with self._lock:
            if not self._started:
                self._cancelled = True
                result = ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
                self._owner._forget(self._action.action_id, self)
                return result
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)


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
        on_invalid_grant: Callable[[int], object] | None = None,
    ) -> None:
        self._configured_identity = _text(configured_identity, "configured_identity")
        self._connection_state = connection_state
        self._credential_store = credential_store
        self._provider = provider
        if not isinstance(trace, DiagnosticTraceRecorder):
            raise TypeError("trace must be a DiagnosticTraceRecorder")
        self._trace = trace
        self._on_invalid_grant = on_invalid_grant
        self._prepared_lock = RLock()
        self._prepared: dict[str, _CalendarWriteDispatch] = {}

    def prepare(self, action: FrozenActionProposal) -> ActionDispatchHandle:
        handle = _CalendarWriteDispatch(self, action)
        with self._prepared_lock:
            if action.action_id in self._prepared:
                raise ActionDispatcherError(
                    f"action {action.action_id} is already prepared",
                    may_have_dispatched=True,
                )
            self._prepared[action.action_id] = handle
        return handle

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        with self._prepared_lock:
            handle = self._prepared.get(action_id)
        if handle is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return handle.cancel()

    def _forget(self, action_id: str, handle: _CalendarWriteDispatch) -> None:
        with self._prepared_lock:
            if self._prepared.get(action_id) is handle:
                del self._prepared[action_id]

    def dispatch(self, action: FrozenActionProposal) -> None:
        request = self._parse_request(action)
        context = _CalendarDispatchContext()
        try:
            with self._connection_state.dispatch_lease():
                credential = self._require_current_connection(request)
                self._execute_traced_write(
                    action=action,
                    request=request,
                    credential=credential,
                    context=context,
                )
        except GoogleCalendarWriteProviderError as exc:
            self._raise_provider_error(request, exc)
        except (TraceCapacityError, TraceWriteError, DiagnosticTraceError) as exc:
            self._raise_trace_error(exc)
        except ActionDispatcherError:
            raise
        except Exception as exc:
            raise ActionDispatcherError(
                "Calendar provider is unavailable",
                may_have_dispatched=context.provider_started,
            ) from exc

    @staticmethod
    def _parse_request(action: FrozenActionProposal) -> CalendarWriteRequest:
        try:
            return CalendarWriteRequest.from_proposal(action)
        except (TypeError, ValueError) as exc:
            raise ActionDispatcherError("Calendar proposal is invalid") from exc

    def _require_current_connection(
        self, request: CalendarWriteRequest
    ) -> OAuthCredentialRecord:
        connection = self._connection_state.get_connection()
        if (
            not connection.connected
            or connection.generation != request.connection_generation
            or CALENDAR_WRITE_SCOPE not in connection.granted_scopes
        ):
            raise ActionDispatcherError("Calendar proposal is stale")
        credential = self._credential_store.current
        if (
            credential is None
            or credential.subject != self._configured_identity
            or CALENDAR_WRITE_SCOPE not in credential.granted_scopes
            or credential.connection_generation != connection.generation
        ):
            raise ActionDispatcherError("Calendar connection is unavailable")
        return credential

    def _execute_traced_write(
        self,
        *,
        action: FrozenActionProposal,
        request: CalendarWriteRequest,
        credential: OAuthCredentialRecord,
        context: _CalendarDispatchContext,
    ) -> None:
        def invoke_provider() -> GoogleCalendarWriteProviderResult:
            context.provider_started = True
            return self._provider.write(request=request, credential=credential)

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

    def _raise_provider_error(
        self, request: CalendarWriteRequest, error: GoogleCalendarWriteProviderError
    ) -> None:
        if error.code == "invalid_grant" and self._on_invalid_grant is not None:
            try:
                self._on_invalid_grant(request.connection_generation)
            except (AuditWriteError, GoogleOAuthError, OSError) as cleanup_error:
                raise ActionDispatcherError(
                    "Calendar credential invalidation failed",
                    may_have_dispatched=False,
                ) from cleanup_error
        raise ActionDispatcherError(
            str(error), may_have_dispatched=error.may_have_dispatched
        ) from error

    @staticmethod
    def _raise_trace_error(error: BaseException) -> None:
        if isinstance(error, TraceWriteError):
            raise ActionDispatcherError(
                "Calendar trace retention failed",
                may_have_dispatched=error.operation_started,
            ) from error
        raise ActionDispatcherError(
            "Calendar trace admission is unavailable"
        ) from error


def build_live_calendar_action_dispatcher(
    *,
    configured_identity: str,
    connection_state: GoogleOAuthStateStore,
    credential_store: GoogleCredentialStore,
    on_invalid_grant: Callable[[int], object],
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


def _contains_complete(
    expected: object, actual: object, *, path: tuple[str, ...] = ()
) -> bool:
    """Accept provider-added fields without making unordered Calendar collections positional."""

    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual
            and _contains_complete(value, actual[key], path=(*path, str(key)))
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        if path in _UNORDERED_COLLECTION_PATHS:
            return _contains_unordered_collection(expected, actual, path=path)
        return all(
            _contains_complete(left, right, path=(*path, str(index)))
            for index, (left, right) in enumerate(zip(expected, actual))
        )
    if (
        path[:2] == ("attendees", "item")
        and path[-1] == "email"
        and isinstance(expected, str)
        and isinstance(actual, str)
    ):
        return expected.casefold() == actual.casefold()
    return expected == actual


def _contains_unordered_collection(
    expected: list[object], actual: list[object], *, path: tuple[str, ...]
) -> bool:
    unmatched = list(actual)
    for expected_item in expected:
        expected_key = _collection_item_key(expected_item, path=path)
        match_index = next(
            (
                index
                for index, actual_item in enumerate(unmatched)
                if _collection_item_key(actual_item, path=path) == expected_key
                and _contains_complete(expected_item, actual_item, path=(*path, "item"))
            ),
            None,
        )
        if match_index is None:
            return False
        unmatched.pop(match_index)
    return not unmatched


def _collection_item_key(value: object, *, path: tuple[str, ...]) -> object:
    if path == ("attendees",) and isinstance(value, Mapping):
        email = value.get("email")
        if isinstance(email, str):
            return ("email", email.casefold())
    if path == ("reminders", "overrides") and isinstance(value, Mapping):
        method = value.get("method")
        minutes = value.get("minutes")
        if isinstance(method, str) and isinstance(minutes, int):
            return ("reminder", method, minutes)
    if path == ("attachments",) and isinstance(value, Mapping):
        file_url = value.get("fileUrl")
        if isinstance(file_url, str):
            return ("attachment", "fileUrl", file_url)
        file_id = value.get("fileId")
        if isinstance(file_id, str):
            return ("attachment", "fileId", file_id)
    if path == ("conferenceData", "entryPoints") and isinstance(value, Mapping):
        entry_point_type = value.get("entryPointType")
        uri = value.get("uri")
        if isinstance(entry_point_type, str) and isinstance(uri, str):
            return ("entryPoint", entry_point_type, uri)
    if path == ("recurrence",) and isinstance(value, str):
        return ("recurrence", value)
    return ("value", json.dumps(value, ensure_ascii=False, sort_keys=True))
