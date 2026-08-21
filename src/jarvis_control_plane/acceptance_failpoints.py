"""Reviewed, application-level post-dispatch fault injection for acceptance.

This boundary deliberately does not interrupt a socket, proxy, firewall, or
container.  It runs after a provider has returned and asks the owning
connector to classify that otherwise successful edge as outcome-unknown.  The
only production construction path is the reviewed active configuration; the
model and the messaging control grammar have no access to this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Literal

AcceptanceFailpointService = Literal["gmail", "calendar"]

_CONFIG_KEYS = frozenset({"enabled", "service", "operation", "action_id", "review_id"})
_SERVICES = frozenset({"gmail", "calendar"})
_OPERATIONS: Mapping[str, frozenset[str]] = {
    "gmail": frozenset({"gmail_send", "gmail_reply"}),
    "calendar": frozenset({"insert", "update", "patch"}),
}


def _safe_identifier(value: object, name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-blank canonical string")
    if len(value) > maximum or any(
        character not in "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
        for character in value
    ):
        raise ValueError(f"{name} must be a bounded safe identifier")
    return value


@dataclass(frozen=True, slots=True)
class ReviewedPostDispatchFailpointSpec:
    """One reviewed, exact acceptance target.

    ``review_id`` is a non-secret human/reviewer reference.  It is retained in
    protected in-memory metadata and never sent to a provider.  The operation
    and action identifier are intentionally exact; wildcards and prefixes are
    not accepted.
    """

    service: AcceptanceFailpointService
    operation: str
    action_id: str
    review_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.service, str) or self.service not in _SERVICES:
            raise ValueError("acceptance failpoint service is not allowed")
        operation = _safe_identifier(self.operation, "acceptance failpoint operation")
        action_id = _safe_identifier(self.action_id, "acceptance failpoint action_id")
        review_id = _safe_identifier(
            self.review_id, "acceptance failpoint review_id", maximum=128
        )
        if operation not in _OPERATIONS[self.service]:
            raise ValueError(
                "acceptance failpoint operation does not belong to its service"
            )
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "review_id", review_id)


class ReviewedPostDispatchFailure(RuntimeError):
    """Private connector signal that a reviewed failpoint fired once."""

    def __init__(self, spec: ReviewedPostDispatchFailpointSpec) -> None:
        self.spec = spec
        super().__init__(
            f"reviewed {spec.service} post-dispatch failpoint {spec.review_id} fired"
        )


class ReviewedPostDispatchFailpoint:
    """A one-shot exact-match failpoint owned by one Google connector."""

    def __init__(self, spec: ReviewedPostDispatchFailpointSpec) -> None:
        if not isinstance(spec, ReviewedPostDispatchFailpointSpec):
            raise TypeError("acceptance failpoint requires a reviewed specification")
        self._spec = spec
        self._lock = Lock()
        self._consumed = False

    @property
    def spec(self) -> ReviewedPostDispatchFailpointSpec:
        return self._spec

    @property
    def consumed(self) -> bool:
        with self._lock:
            return self._consumed

    def raise_if_armed(
        self,
        *,
        service: AcceptanceFailpointService,
        operation: str,
        action_id: str,
    ) -> None:
        """Raise only for the configured service, operation, and action once."""

        if (
            service != self._spec.service
            or operation != self._spec.operation
            or action_id != self._spec.action_id
        ):
            return
        with self._lock:
            if self._consumed:
                return
            self._consumed = True
        raise ReviewedPostDispatchFailure(self._spec)

    def metadata(self) -> Mapping[str, str]:
        """Return bounded, non-secret evidence suitable for protected review."""

        return {
            "service": self._spec.service,
            "operation": self._spec.operation,
            "action_id": self._spec.action_id,
            "review_id": self._spec.review_id,
            "state": "consumed" if self.consumed else "armed",
        }


def reviewed_post_dispatch_failpoint_from_config(
    value: object,
) -> ReviewedPostDispatchFailpoint | None:
    """Parse the optional active-config section, with disabled as the default."""

    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _CONFIG_KEYS:
        raise ValueError(
            "acceptance_failpoint must define exactly enabled, service, operation, "
            "action_id, and review_id"
        )
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise TypeError("acceptance_failpoint.enabled must be a boolean")

    service = value.get("service")
    operation = value.get("operation")
    action_id = value.get("action_id")
    review_id = value.get("review_id")
    if not all(
        isinstance(item, str) for item in (service, operation, action_id, review_id)
    ):
        raise ValueError("acceptance_failpoint fields must be strings")
    if not enabled:
        if any(item != "" for item in (service, operation, action_id, review_id)):
            raise ValueError(
                "disabled acceptance_failpoint must use empty target fields"
            )
        return None

    spec = ReviewedPostDispatchFailpointSpec(
        service=service,  # type: ignore[arg-type]
        operation=operation,
        action_id=action_id,
        review_id=review_id,
    )
    return ReviewedPostDispatchFailpoint(spec)


__all__ = [
    "AcceptanceFailpointService",
    "ReviewedPostDispatchFailpoint",
    "ReviewedPostDispatchFailpointSpec",
    "ReviewedPostDispatchFailure",
    "reviewed_post_dispatch_failpoint_from_config",
]
