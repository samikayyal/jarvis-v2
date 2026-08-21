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
        "action_id": "ticket31-gmail-send-01",
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


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"enabled": True},
        {
            "enabled": True,
            "service": "gmail",
            "operation": "gmail_send",
            "action_id": "ticket31-*",
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
