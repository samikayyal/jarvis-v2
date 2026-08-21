"""Reviewed, application-level post-dispatch fault injection for acceptance.

This boundary deliberately does not interrupt a socket, proxy, firewall, or
container.  It runs after a provider has returned and asks the owning
connector to classify that otherwise successful edge as outcome-unknown.  The
only production construction path is the reviewed active configuration; the
model and the messaging control grammar have no access to this module.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

AcceptanceFailpointService = Literal["gmail", "calendar"]

_CONFIG_KEYS = frozenset({"enabled", "service", "operation", "action_id", "review_id"})
_SERVICES = frozenset({"gmail", "calendar"})
_OPERATIONS: Mapping[str, frozenset[str]] = {
    "gmail": frozenset({"gmail_send", "gmail_reply"}),
    "calendar": frozenset({"insert", "update", "patch"}),
}
_MARKER_VERSION = "1"
_MAX_MARKER_BYTES = 4096


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
    and any bound action identifier are intentionally exact; wildcards and
    prefixes are not accepted.  An empty action identifier is the reviewed
    request-scoped arm that is bound exactly once by the connector owner.
    """

    service: AcceptanceFailpointService
    operation: str
    action_id: str | None
    review_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.service, str) or self.service not in _SERVICES:
            raise ValueError("acceptance failpoint service is not allowed")
        operation = _safe_identifier(self.operation, "acceptance failpoint operation")
        action_id = self.action_id
        if action_id == "":
            action_id = None
        elif action_id is not None:
            action_id = _safe_identifier(action_id, "acceptance failpoint action_id")
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

    def __init__(
        self,
        spec: ReviewedPostDispatchFailpointSpec,
        *,
        action_id: str | None = None,
    ) -> None:
        self.spec = spec
        self.action_id = action_id or spec.action_id
        super().__init__(
            f"reviewed {spec.service} post-dispatch failpoint {spec.review_id} fired"
        )


class ReviewedPostDispatchFailpoint:
    """A one-shot exact-match failpoint owned by one Google connector."""

    def __init__(
        self,
        spec: ReviewedPostDispatchFailpointSpec,
        *,
        durable_root: Path | str | None = None,
    ) -> None:
        if not isinstance(spec, ReviewedPostDispatchFailpointSpec):
            raise TypeError("acceptance failpoint requires a reviewed specification")
        self._spec = spec
        if durable_root is not None and not isinstance(durable_root, (Path, str)):
            raise TypeError("acceptance failpoint durable_root must be a path")
        self._durable_root = Path(durable_root) if durable_root is not None else None
        self._lock = Lock()
        self._consumed = False
        self._inert = False
        self._bound_action_id = spec.action_id
        self._load_durable_binding()

    @property
    def spec(self) -> ReviewedPostDispatchFailpointSpec:
        return self._spec

    @property
    def consumed(self) -> bool:
        with self._lock:
            return self._consumed

    @property
    def inert(self) -> bool:
        with self._lock:
            return self._inert

    @property
    def bound_action_id(self) -> str | None:
        with self._lock:
            return self._bound_action_id

    def bind_action(
        self,
        *,
        service: AcceptanceFailpointService,
        operation: str,
        action_id: str,
    ) -> bool:
        """Bind an unbound review to the connector-owned frozen action.

        The Google service calls this after its connector has returned the
        bound proposal and before the broker can prepare it.  The model and
        chat control surface therefore cannot choose or retarget the action
        that the review arms.  A durable binding marker makes that choice
        survive a process restart.
        """

        if service != self._spec.service or operation != self._spec.operation:
            return False
        action_id = _safe_identifier(action_id, "acceptance failpoint action_id")
        with self._lock:
            if self._inert or self._consumed:
                return False
            if self._bound_action_id is not None:
                return self._bound_action_id == action_id
            if self._durable_root is None:
                # Direct connector tests may construct the seam without a
                # durable root.  Production composition always supplies one.
                self._bound_action_id = action_id
                return True
            if not self._ensure_durable_root_locked():
                self._inert = True
                return False
            marker = self._binding_marker_path_locked()
            payload = _marker_payload(self._spec, action_id, state="bound")
            created = _create_marker(marker, payload)
            if created is True:
                self._bound_action_id = action_id
                if not self._sync_directory_locked():
                    self._inert = True
                    return False
                return True
            if created is None:
                self._inert = True
                return False
            try:
                persisted = _read_marker(
                    marker, expected_state="bound", expected_spec=self._spec
                )
            except (OSError, ValueError):
                self._inert = True
                return False
            if persisted != action_id:
                self._inert = True
                return False
            self._bound_action_id = persisted
            self._load_consumed_locked()
            return True

    def raise_if_armed(
        self,
        *,
        service: AcceptanceFailpointService,
        operation: str,
        action_id: str,
    ) -> None:
        """Raise only for the configured service, operation, and action once."""

        if service != self._spec.service or operation != self._spec.operation:
            return
        if not isinstance(action_id, str) or action_id.strip() != action_id:
            return
        with self._lock:
            if (
                self._inert
                or self._consumed
                or self._bound_action_id is None
                or action_id != self._bound_action_id
            ):
                return
            if self._durable_root is None:
                self._consumed = True
                should_raise = True
            else:
                should_raise = self._claim_consumption_locked()
        if should_raise:
            raise ReviewedPostDispatchFailure(self._spec, action_id=action_id)

    def metadata(self) -> Mapping[str, str]:
        """Return bounded, non-secret evidence suitable for protected review."""

        return {
            "service": self._spec.service,
            "operation": self._spec.operation,
            "action_id": self.bound_action_id or "",
            "review_id": self._spec.review_id,
            "state": (
                "inert"
                if self.inert
                else "consumed"
                if self.consumed
                else "bound"
                if self.bound_action_id is not None
                else "armed"
            ),
        }

    def _load_durable_binding(self) -> None:
        if self._durable_root is None:
            return
        marker = self._binding_marker_path()
        try:
            if not marker.exists():
                return
            persisted = _read_marker(
                marker, expected_state="bound", expected_spec=self._spec
            )
        except (OSError, ValueError):
            self._inert = True
            return
        if self._bound_action_id is not None and persisted != self._bound_action_id:
            self._inert = True
            return
        self._bound_action_id = persisted
        self._load_consumed_locked()

    def _load_consumed_locked(self) -> None:
        if self._durable_root is None or self._bound_action_id is None:
            return
        marker = self._consumed_marker_path_locked()
        try:
            if marker.exists():
                if (
                    _read_marker(
                        marker, expected_state="consumed", expected_spec=self._spec
                    )
                    != self._bound_action_id
                ):
                    self._inert = True
                    return
                self._consumed = True
        except (OSError, ValueError):
            self._inert = True

    def _ensure_durable_root_locked(self) -> bool:
        assert self._durable_root is not None
        try:
            self._durable_root.mkdir(parents=True, exist_ok=True)
            return self._durable_root.is_dir()
        except OSError:
            return False

    def _binding_marker_path(self) -> Path:
        assert self._durable_root is not None
        return self._durable_root / f"{_scope_digest(self._spec)}.binding"

    def _binding_marker_path_locked(self) -> Path:
        return self._binding_marker_path()

    def _consumed_marker_path_locked(self) -> Path:
        assert self._durable_root is not None
        assert self._bound_action_id is not None
        return self._durable_root / (
            f"{_scope_digest(self._spec, self._bound_action_id)}.consumed"
        )

    def _claim_consumption_locked(self) -> bool:
        if not self._ensure_durable_root_locked():
            self._inert = True
            return False
        marker = self._consumed_marker_path_locked()
        payload = _marker_payload(
            self._spec, self._bound_action_id or "", state="consumed"
        )
        created = _create_marker(marker, payload)
        if created is True:
            self._consumed = True
            if not self._sync_directory_locked():
                self._consumed = False
                self._inert = True
                return False
            return True
        if created is None:
            self._inert = True
            return False
        try:
            persisted = _read_marker(
                marker, expected_state="consumed", expected_spec=self._spec
            )
        except (OSError, ValueError):
            self._inert = True
            return False
        if persisted != self._bound_action_id:
            self._inert = True
            return False
        self._consumed = True
        return False

    def _sync_directory_locked(self) -> bool:
        if self._durable_root is None or os.name == "nt":
            return True
        try:
            descriptor = os.open(
                self._durable_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            return False
        return True


def _scope_digest(
    spec: ReviewedPostDispatchFailpointSpec, action_id: str | None = None
) -> str:
    material = "\0".join(
        (spec.service, spec.operation, spec.review_id, action_id or "")
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()


def _marker_payload(
    spec: ReviewedPostDispatchFailpointSpec, action_id: str, *, state: str
) -> bytes:
    return (
        f"version={_MARKER_VERSION}\n"
        f"service={spec.service}\n"
        f"operation={spec.operation}\n"
        f"review_id={spec.review_id}\n"
        f"action_id={action_id}\n"
        f"state={state}\n"
    ).encode("ascii")


def _create_marker(path: Path, payload: bytes) -> bool | None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return False
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        return None
    return True


def _read_marker(
    path: Path,
    *,
    expected_state: str,
    expected_spec: ReviewedPostDispatchFailpointSpec | None = None,
) -> str:
    fields: dict[str, str] = {}
    if path.stat().st_size > _MAX_MARKER_BYTES:
        raise ValueError("acceptance failpoint marker is too large")
    raw = path.read_bytes()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("acceptance failpoint marker is not ASCII") from exc
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in fields:
            raise ValueError("acceptance failpoint marker is malformed")
        fields[key] = value
    if set(fields) != {
        "version",
        "service",
        "operation",
        "review_id",
        "action_id",
        "state",
    }:
        raise ValueError("acceptance failpoint marker shape is invalid")
    if fields["version"] != _MARKER_VERSION or fields["state"] != expected_state:
        raise ValueError("acceptance failpoint marker state is invalid")
    if expected_spec is not None and (
        fields["service"] != expected_spec.service
        or fields["operation"] != expected_spec.operation
        or fields["review_id"] != expected_spec.review_id
    ):
        raise ValueError("acceptance failpoint marker scope is invalid")
    _safe_identifier(fields["service"], "acceptance failpoint marker service")
    _safe_identifier(fields["operation"], "acceptance failpoint marker operation")
    _safe_identifier(fields["review_id"], "acceptance failpoint marker review_id")
    return _safe_identifier(
        fields["action_id"], "acceptance failpoint marker action_id"
    )


def reviewed_post_dispatch_failpoint_from_config(
    value: object,
    *,
    durable_root: Path | str | None = None,
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

    if action_id != "":
        raise ValueError(
            "enabled acceptance_failpoint action_id must be empty and binds to "
            "the connector-owned frozen action"
        )

    spec = ReviewedPostDispatchFailpointSpec(
        service=service,  # type: ignore[arg-type]
        operation=operation,
        action_id=None,
        review_id=review_id,
    )
    return ReviewedPostDispatchFailpoint(spec, durable_root=durable_root)


__all__ = [
    "AcceptanceFailpointService",
    "ReviewedPostDispatchFailpoint",
    "ReviewedPostDispatchFailpointSpec",
    "ReviewedPostDispatchFailure",
    "reviewed_post_dispatch_failpoint_from_config",
]
