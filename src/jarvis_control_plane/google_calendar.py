"""Exact, approval-gated Google Calendar create and change boundary.

The capability broker retains the approval and once-only dispatch transaction.
This module owns only the Calendar-specific half: producing a closed frozen
proposal and refusing to call a provider when its Calendar state is stale.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from .google_oauth import (
    GoogleCredentialStore,
    GoogleOAuthStateStore,
    OAuthCredentialRecord,
)
from .models import FrozenActionProposal
from .ports import ActionDispatcherError

CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CalendarWriteOperation = Literal["insert", "update", "patch"]
CalendarNotification = Literal["none", "all", "externalOnly"]
_WRITE_KINDS = frozenset({"calendar_insert", "calendar_update", "calendar_patch"})
_ARRAY_FIELDS = frozenset({"attendees", "recurrence", "reminders"})


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

    def __init__(self, message: str, *, may_have_dispatched: bool = False) -> None:
        super().__init__(message)
        self.may_have_dispatched = may_have_dispatched


class GoogleCalendarWriteProvider(Protocol):
    """Fixed create/update/patch provider edge; delete and calendar mutation are absent."""

    def write(
        self, *, request: CalendarWriteRequest, credential: OAuthCredentialRecord
    ) -> None: ...


class ControlledGoogleCalendarWriteProvider:
    """Controlled Calendar edge for approval and failure-contract tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[CalendarWriteRequest, OAuthCredentialRecord]] = []
        self.failure: tuple[str, bool] | None = None

    def write(
        self, *, request: CalendarWriteRequest, credential: OAuthCredentialRecord
    ) -> None:
        self.calls.append((request, credential))
        if self.failure is not None:
            message, may_have_dispatched = self.failure
            raise GoogleCalendarWriteProviderError(
                message, may_have_dispatched=may_have_dispatched
            )


class CalendarActionDispatcher:
    """Dispatches only a current, frozen Calendar write through the closed provider."""

    def __init__(
        self,
        *,
        configured_identity: str,
        connection_state: GoogleOAuthStateStore,
        credential_store: GoogleCredentialStore,
        provider: GoogleCalendarWriteProvider,
    ) -> None:
        self._configured_identity = _text(configured_identity, "configured_identity")
        self._connection_state = connection_state
        self._credential_store = credential_store
        self._provider = provider

    def dispatch(self, action: FrozenActionProposal) -> None:
        try:
            request = CalendarWriteRequest.from_proposal(action)
        except (TypeError, ValueError) as exc:
            raise ActionDispatcherError("Calendar proposal is invalid") from exc
        try:
            connection = self._connection_state.get_connection()
            credential = self._credential_store.current
        except Exception as exc:
            raise ActionDispatcherError("Calendar connection is unavailable") from exc
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
        ):
            raise ActionDispatcherError("Calendar connection is unavailable")
        try:
            self._provider.write(request=request, credential=credential)
        except GoogleCalendarWriteProviderError as exc:
            raise ActionDispatcherError(
                str(exc), may_have_dispatched=exc.may_have_dispatched
            ) from exc
        except Exception as exc:
            raise ActionDispatcherError("Calendar provider is unavailable") from exc
