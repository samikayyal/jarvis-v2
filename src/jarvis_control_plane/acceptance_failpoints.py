"""Reviewed, one-shot application failpoints for supervised acceptance runs.

The failpoint is deliberately independent of transport, proxy, and container
state.  A connector calls it only after its provider operation has returned and
before it records the terminal acknowledgement.  Production composition gives
it a durable marker directory so a static enabled configuration cannot arm the
same reviewed action again after a process restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Literal


_SERVICES = frozenset({"gmail", "calendar"})
_OPERATIONS = {
    "gmail": frozenset({"gmail_send", "gmail_reply"}),
    "calendar": frozenset({"insert", "update", "patch"}),
}
_IDENTIFIER_MAX_LENGTH = 256


class ReviewedPostDispatchFailure(RuntimeError):
    """Sanitized signal that a reviewed post-dispatch boundary was tripped."""

    def __init__(self, spec: "ReviewedPostDispatchFailpointSpec") -> None:
        self.spec = spec
        super().__init__("reviewed post-dispatch failpoint tripped")


@dataclass(frozen=True, slots=True)
class ReviewedPostDispatchFailpointSpec:
    """Closed, exact target for one reviewed provider operation."""

    service: Literal["gmail", "calendar"]
    operation: str
    action_id: str
    review_id: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("failpoint enabled must be boolean")
        if not isinstance(self.service, str) or self.service not in _SERVICES:
            raise ValueError("failpoint service is invalid")
        if (
            not isinstance(self.operation, str)
            or self.operation not in _OPERATIONS[self.service]
        ):
            raise ValueError("failpoint operation is invalid for its service")
        _identifier(self.action_id, "failpoint action_id")
        _identifier(self.review_id, "failpoint review_id")


class ReviewedPostDispatchFailpoint:
    """Thread-safe, exact, one-shot application failpoint.

    ``consumption_root`` is optional for deterministic unit tests.  The
    production Google connector supplies a persistent directory; claiming a
    marker there with exclusive creation is the cross-process/restart guard.
    """

    def __init__(
        self,
        spec: ReviewedPostDispatchFailpointSpec,
        *,
        consumption_root: str | Path | None = None,
    ) -> None:
        if not isinstance(spec, ReviewedPostDispatchFailpointSpec):
            raise TypeError("failpoint spec is invalid")
        self.spec = spec
        self._lock = RLock()
        self._consumption_root = (
            None if consumption_root is None else Path(consumption_root)
        )
        if (
            self._consumption_root is not None
            and not self._consumption_root.is_absolute()
        ):
            raise ValueError("failpoint consumption root must be absolute")
        self._marker_path = (
            None
            if self._consumption_root is None
            else self._consumption_root
            / f"{self._marker_name()}.consumed"
        )
        self._consumed = self._marker_exists()

    @property
    def consumed(self) -> bool:
        """Whether this exact reviewed target has been durably consumed."""

        with self._lock:
            if self._consumed:
                return True
            self._consumed = self._marker_exists()
            return self._consumed

    def raise_if_armed(
        self, *, service: str, operation: str, action_id: str
    ) -> None:
        """Trip once for the exact service, operation, and action target."""

        if not self.spec.enabled or (
            service != self.spec.service
            or operation != self.spec.operation
            or action_id != self.spec.action_id
        ):
            return
        with self._lock:
            if self._consumed:
                return
            if self._marker_path is not None and not self._claim_marker():
                self._consumed = self._marker_exists()
                return
            self._consumed = True
        raise ReviewedPostDispatchFailure(self.spec)

    def _marker_name(self) -> str:
        canonical = json.dumps(
            {
                "action_id": self.spec.action_id,
                "operation": self.spec.operation,
                "review_id": self.spec.review_id,
                "service": self.spec.service,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(canonical).hexdigest()

    def _marker_exists(self) -> bool:
        return self._marker_path is not None and self._marker_path.is_file()

    def _claim_marker(self) -> bool:
        """Atomically claim this exact target across threads/processes."""

        assert self._marker_path is not None
        try:
            self._marker_path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._marker_path, flags | no_follow, 0o600)
        except FileExistsError:
            return False
        except OSError:
            # A fault-injection arm that cannot be durably consumed is inert;
            # it must never create an unknown outcome without the one-shot
            # restart guard.
            return False
        try:
            marker = b"reviewed-post-dispatch-consumed\n"
            if os.write(fd, marker) != len(marker):
                raise OSError("consumption marker write was incomplete")
            os.fsync(fd)
        except OSError:
            try:
                os.unlink(self._marker_path)
            except OSError:
                pass
            return False
        finally:
            os.close(fd)
        return True


def _identifier(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > _IDENTIFIER_MAX_LENGTH
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value) is None
    ):
        raise ValueError(f"{name} is invalid")
    return value
