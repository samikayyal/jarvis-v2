from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import jarvis_control_plane
from jarvis_control_plane.acceptance_failpoints import (
    ReviewedPostDispatchFailpointSpec,
)
from jarvis_control_plane.action_dispatch import RoutedActionDispatcher
from jarvis_control_plane.google_oauth import GOOGLE_OAUTH_SCOPES
from jarvis_control_plane.models import FrozenActionProposal
from jarvis_control_plane.orchestration import AgentsSdkProposal
from jarvis_control_plane.ports import ActionDispatcherError
from jarvis_control_plane.service_runtime import (
    _GOOGLE_AUTHORIZATION_ACCESS_SCOPES,
    SERVICE_ROLES,
    _RemoteGoogleReads,
)


def test_v1_package_and_protocol_do_not_expose_calendar() -> None:
    assert not any("Calendar" in name for name in jarvis_control_plane.__all__)
    assert not any(
        "calendar" in operation
        for operation in SERVICE_ROLES["google_connector"].operations
    )
    assert not hasattr(_RemoteGoogleReads, "calendar_list")
    assert not hasattr(_RemoteGoogleReads, "calendar_events_list")
    assert not hasattr(_RemoteGoogleReads, "calendar_events_get")


def test_v1_authorization_and_configuration_do_not_offer_calendar() -> None:
    assert not any("/auth/calendar" in scope for scope in GOOGLE_OAUTH_SCOPES)
    assert set(_GOOGLE_AUTHORIZATION_ACCESS_SCOPES) == {"baseline", "gmail-send"}
    assert not any(
        "/auth/calendar" in scope
        for scopes in _GOOGLE_AUTHORIZATION_ACCESS_SCOPES.values()
        for scope in scopes
    )
    config = (Path(__file__).parents[1] / "deployment/config.example.toml").read_text()
    assert "calendar =" not in config


@pytest.mark.parametrize(
    "kind", ("calendar_insert", "calendar_update", "calendar_patch")
)
def test_v1_structured_output_rejects_calendar_proposals(kind: str) -> None:
    with pytest.raises(ValidationError):
        AgentsSdkProposal(kind=kind, preview="Calendar", payload={})


def test_v1_broker_has_no_calendar_dispatch_route() -> None:
    dispatcher = RoutedActionDispatcher(
        terminal=SimpleNamespace(),
        gmail=SimpleNamespace(),
        gmail_lifecycle=SimpleNamespace(),
    )
    action = FrozenActionProposal.create(
        action_id="calendar-action",
        request_id="calendar-request",
        kind="calendar_insert",
        preview="Calendar",
        payload={},
    )

    with pytest.raises(ActionDispatcherError, match="no action dispatcher"):
        dispatcher.prepare(action)


def test_v1_failpoint_cannot_target_calendar() -> None:
    with pytest.raises(ValueError, match="service is not allowed"):
        ReviewedPostDispatchFailpointSpec(
            service="calendar",  # type: ignore[arg-type]
            operation="insert",
            action_id="",
            review_id="calendar-is-not-v1",
        )
