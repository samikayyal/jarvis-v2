from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from jarvis_control_plane.acceptance_failpoints import (
    ReviewedPostDispatchFailpoint,
    ReviewedPostDispatchFailpointSpec,
    ReviewedPostDispatchFailure,
    reviewed_post_dispatch_failpoint_from_config,
)
from jarvis_control_plane.deployment import (
    BundleValidationError,
    validate_configuration,
)


def _config() -> dict[str, object]:
    return tomllib.loads(
        (Path(__file__).parents[1] / "deployment/config.example.toml").read_text(
            encoding="utf-8"
        )
    )


def _enabled_config() -> dict[str, object]:
    config = _config()
    config["acceptance_failpoint"] = {
        "enabled": True,
        "service": "gmail",
        "operation": "gmail_send",
        "action_id": "",
        "review_id": "ticket31-gmail-unknown",
    }
    return config


def test_absent_and_explicitly_disabled_configuration_are_inert() -> None:
    assert reviewed_post_dispatch_failpoint_from_config(None) is None
    assert (
        reviewed_post_dispatch_failpoint_from_config(
            {
                "enabled": False,
                "service": "",
                "operation": "",
                "action_id": "",
                "review_id": "",
            }
        )
        is None
    )


def test_exact_match_consumes_once_and_mismatches_preserve_the_armed_target() -> None:
    failpoint = ReviewedPostDispatchFailpoint(
        ReviewedPostDispatchFailpointSpec(
            service="gmail",
            operation="gmail_send",
            action_id="ticket31-gmail-send-01",
            review_id="ticket31-gmail-unknown",
        )
    )

    failpoint.raise_if_armed(
        service="gmail", operation="gmail_reply", action_id="ticket31-gmail-send-01"
    )
    failpoint.raise_if_armed(
        service="calendar", operation="gmail_send", action_id="ticket31-gmail-send-01"
    )
    assert failpoint.consumed is False

    with pytest.raises(ReviewedPostDispatchFailure) as raised:
        failpoint.raise_if_armed(
            service="gmail",
            operation="gmail_send",
            action_id="ticket31-gmail-send-01",
        )

    assert raised.value.spec.review_id == "ticket31-gmail-unknown"
    assert failpoint.consumed is True
    failpoint.raise_if_armed(
        service="gmail", operation="gmail_send", action_id="ticket31-gmail-send-01"
    )


def test_request_scoped_arm_binds_the_frozen_action_and_survives_restart(
    tmp_path: Path,
) -> None:
    spec = ReviewedPostDispatchFailpointSpec(
        service="gmail",
        operation="gmail_send",
        action_id="",
        review_id="ticket31-gmail-unknown-restart",
    )
    first = ReviewedPostDispatchFailpoint(spec, durable_root=tmp_path)

    assert first.bind_action(
        service="gmail",
        operation="gmail_send",
        action_id="request-real:proposal",
    )
    first.raise_if_armed(
        service="gmail", operation="gmail_send", action_id="request-other:proposal"
    )

    restarted = ReviewedPostDispatchFailpoint(spec, durable_root=tmp_path)
    assert restarted.bound_action_id == "request-real:proposal"
    with pytest.raises(ReviewedPostDispatchFailure):
        restarted.raise_if_armed(
            service="gmail",
            operation="gmail_send",
            action_id="request-real:proposal",
        )
    assert restarted.consumed is True

    third_process = ReviewedPostDispatchFailpoint(spec, durable_root=tmp_path)
    third_process.bind_action(
        service="gmail",
        operation="gmail_send",
        action_id="request-real:proposal",
    )
    third_process.raise_if_armed(
        service="gmail",
        operation="gmail_send",
        action_id="request-real:proposal",
    )
    assert third_process.consumed is True


def test_request_scoped_arm_is_inert_when_durable_claim_is_unavailable(
    tmp_path: Path,
) -> None:
    durable_root = tmp_path / "not-a-directory"
    durable_root.write_text("occupied", encoding="utf-8")
    failpoint = ReviewedPostDispatchFailpoint(
        ReviewedPostDispatchFailpointSpec(
            service="gmail",
            operation="gmail_send",
            action_id="",
            review_id="ticket31-gmail-unknown-inert",
        ),
        durable_root=durable_root,
    )

    assert (
        failpoint.bind_action(
            service="gmail", operation="gmail_send", action_id="request:proposal"
        )
        is False
    )
    failpoint.raise_if_armed(
        service="gmail", operation="gmail_send", action_id="request:proposal"
    )
    assert failpoint.consumed is False
    assert failpoint.inert is True


def test_google_service_composition_binds_the_actual_frozen_action() -> None:
    import jarvis_control_plane.service_runtime as runtime
    from jarvis_control_plane.models import FrozenActionProposal

    failpoint = ReviewedPostDispatchFailpoint(
        ReviewedPostDispatchFailpointSpec(
            service="gmail",
            operation="gmail_send",
            action_id="",
            review_id="ticket31-gmail-composition",
        )
    )
    action = FrozenActionProposal.create(
        action_id="request-real:proposal",
        request_id="request-real",
        kind="gmail_send",
        preview="Send the reviewed message.",
        payload={
            "schema": "gmail_write_v1",
            "operation": "gmail_send",
            "to": ["operator@example.test"],
            "cc": [],
            "bcc": [],
            "subject": "Acceptance",
            "body": "Reviewed",
            "mime_type": "text/plain",
        },
    )

    class Owner:
        def bind_proposal(
            self, candidate: FrozenActionProposal
        ) -> FrozenActionProposal:
            return candidate

    dispatcher = runtime._GoogleActionDispatcher(
        gmail=Owner(), acceptance_failpoint=failpoint
    )
    bound = dispatcher.bind_proposal(action)

    assert bound.action_id == "request-real:proposal"
    assert failpoint.bound_action_id == "request-real:proposal"


def test_google_service_composition_fails_closed_after_consumption() -> None:
    import jarvis_control_plane.service_runtime as runtime
    from jarvis_control_plane.models import FrozenActionProposal

    failpoint = ReviewedPostDispatchFailpoint(
        ReviewedPostDispatchFailpointSpec(
            service="gmail",
            operation="gmail_send",
            action_id="",
            review_id="ticket31-gmail-composition-consumed",
        )
    )
    first_action = FrozenActionProposal.create(
        action_id="request-first:proposal",
        request_id="request-first",
        kind="gmail_send",
        preview="Send the first reviewed message.",
        payload={
            "schema": "gmail_write_v1",
            "operation": "gmail_send",
            "to": ["operator@example.test"],
            "cc": [],
            "bcc": [],
            "subject": "Acceptance",
            "body": "First",
            "mime_type": "text/plain",
        },
    )
    second_action = FrozenActionProposal.create(
        action_id="request-second:proposal",
        request_id="request-second",
        kind="gmail_send",
        preview="Send the second reviewed message.",
        payload={
            "schema": "gmail_write_v1",
            "operation": "gmail_send",
            "to": ["operator@example.test"],
            "cc": [],
            "bcc": [],
            "subject": "Acceptance",
            "body": "Second",
            "mime_type": "text/plain",
        },
    )

    class Owner:
        def bind_proposal(
            self, candidate: FrozenActionProposal
        ) -> FrozenActionProposal:
            return candidate

    dispatcher = runtime._GoogleActionDispatcher(
        gmail=Owner(), acceptance_failpoint=failpoint
    )
    assert dispatcher.bind_proposal(first_action).action_id == "request-first:proposal"
    with pytest.raises(ReviewedPostDispatchFailure):
        failpoint.raise_if_armed(
            service="gmail",
            operation="gmail_send",
            action_id="request-first:proposal",
        )

    with pytest.raises(ValueError, match="bind the frozen action durably"):
        dispatcher.bind_proposal(second_action)


def test_google_service_composition_rejects_matching_arm_without_durable_binding(
    tmp_path: Path,
) -> None:
    import jarvis_control_plane.service_runtime as runtime
    from jarvis_control_plane.models import FrozenActionProposal

    durable_root = tmp_path / "not-a-directory"
    durable_root.write_text("occupied", encoding="utf-8")
    failpoint = ReviewedPostDispatchFailpoint(
        ReviewedPostDispatchFailpointSpec(
            service="gmail",
            operation="gmail_send",
            action_id="",
            review_id="ticket31-gmail-composition-inert",
        ),
        durable_root=durable_root,
    )
    action = FrozenActionProposal.create(
        action_id="request-real:proposal",
        request_id="request-real",
        kind="gmail_send",
        preview="Send the reviewed message.",
        payload={
            "schema": "gmail_write_v1",
            "operation": "gmail_send",
            "to": ["operator@example.test"],
            "cc": [],
            "bcc": [],
            "subject": "Acceptance",
            "body": "Reviewed",
            "mime_type": "text/plain",
        },
    )

    class Owner:
        def bind_proposal(
            self, candidate: FrozenActionProposal
        ) -> FrozenActionProposal:
            return candidate

    dispatcher = runtime._GoogleActionDispatcher(
        gmail=Owner(), acceptance_failpoint=failpoint
    )

    with pytest.raises(ValueError, match="bind the frozen action durably"):
        dispatcher.bind_proposal(action)
    assert failpoint.inert is True


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"enabled": True},
        {
            "enabled": True,
            "service": "gmail",
            "operation": "gmail_send",
            "action_id": "ticket31-gmail-send-01",
            "review_id": "review",
        },
        {
            "enabled": True,
            "service": "calendar",
            "operation": "gmail_send",
            "action_id": "ticket31-calendar-write-01",
            "review_id": "review",
        },
        {
            "enabled": False,
            "service": "gmail",
            "operation": "",
            "action_id": "",
            "review_id": "",
            "extra": "rejected",
        },
    ],
)
def test_malformed_or_wildcard_configuration_is_rejected(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        reviewed_post_dispatch_failpoint_from_config(value)


def test_enabled_failpoint_is_rejected_in_example_configuration_but_allowed_in_active() -> (
    None
):
    example = _enabled_config()
    with pytest.raises(BundleValidationError, match="only be enabled in active"):
        validate_configuration(example)

    active = _enabled_config()
    active["configuration_kind"] = "active"
    errors: list[str] = []
    from jarvis_control_plane.deployment import _validate_configuration

    _validate_configuration(active, errors)
    assert (
        "acceptance_failpoint may only be enabled in active configuration" not in errors
    )
