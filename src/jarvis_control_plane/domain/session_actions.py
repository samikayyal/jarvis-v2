"""Approval-gated pending actions and durable dispatch values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256

from .session_core import (
    PENDING_ACTION_TTL,
    Clock,
    InvariantViolation,
    ProposalPresentationStatus,
    _identifier,
    _now,
    ensure_utc,
)


@dataclass(frozen=True, slots=True)
class PendingActionState:
    """One exact, immutable action awaiting its owning operator's decision."""

    action_id: str
    session_id: str
    request_id: str
    kind: str
    summary: str
    created_at: datetime
    expires_at: datetime
    digest: str = ""
    preview: str | None = None
    payload: str = ""
    policy_disposition: str | None = None
    presentation_status: ProposalPresentationStatus | str = (
        ProposalPresentationStatus.PRESENTED
    )
    presentation_fragments: tuple[ProposalPresentationFragment, ...] = ()

    def __post_init__(self) -> None:
        for name in ("action_id", "session_id", "request_id", "kind", "summary"):
            _identifier(getattr(self, name), name)
        created_at = ensure_utc(self.created_at)
        expires_at = ensure_utc(self.expires_at)
        if expires_at <= created_at:
            raise ValueError("pending action expiry must be after creation")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        preview = self.preview if self.preview is not None else self.summary
        _identifier(preview, "preview")
        if not isinstance(self.payload, str):
            raise TypeError("payload must be frozen text")
        expected = _pending_action_digest(
            action_id=self.action_id,
            request_id=self.request_id,
            kind=self.kind,
            preview=preview,
            payload=self.payload,
        )
        if self.digest and self.digest != expected:
            raise ValueError("pending action digest does not match frozen content")
        object.__setattr__(self, "preview", preview)
        object.__setattr__(self, "digest", expected)
        object.__setattr__(
            self,
            "presentation_status",
            ProposalPresentationStatus(self.presentation_status),
        )
        fragments = tuple(self.presentation_fragments)
        if any(
            not isinstance(fragment, ProposalPresentationFragment)
            for fragment in fragments
        ):
            raise TypeError(
                "presentation fragments must be ProposalPresentationFragment values"
            )
        if fragments:
            total = fragments[0].total
            if any(fragment.total != total for fragment in fragments):
                raise InvariantViolation("presentation fragments must have one total")
            if tuple(fragment.number for fragment in fragments) != tuple(
                range(1, len(fragments) + 1)
            ):
                raise InvariantViolation(
                    "presentation fragments must be recorded in order"
                )
            if len(fragments) > total:
                raise InvariantViolation("presentation contains too many fragments")
        object.__setattr__(self, "presentation_fragments", fragments)
        if self.policy_disposition is not None:
            _identifier(self.policy_disposition, "policy_disposition")

    @classmethod
    def create(
        cls,
        *,
        action_id: str,
        session_id: str,
        request_id: str,
        kind: str,
        summary: str,
        created_at: datetime | Clock,
        preview: str | None = None,
        payload: str = "",
        policy_disposition: str | None = None,
        presentation_status: ProposalPresentationStatus
        | str = ProposalPresentationStatus.PRESENTED,
    ) -> PendingActionState:
        created = _now(created_at)
        return cls(
            action_id=action_id,
            session_id=session_id,
            request_id=request_id,
            kind=kind,
            summary=summary,
            created_at=created,
            expires_at=created + PENDING_ACTION_TTL,
            preview=preview,
            payload=payload,
            policy_disposition=policy_disposition,
            presentation_status=presentation_status,
        )

    @classmethod
    def from_proposal(
        cls,
        proposal: object,
        *,
        session_id: str,
        created_at: datetime | Clock,
        presentation_status: ProposalPresentationStatus
        | str = ProposalPresentationStatus.PRESENTED,
        policy_disposition: str | None = None,
    ) -> PendingActionState:
        """Freeze the typed orchestration proposal into durable session state."""

        try:
            action_id = proposal.action_id
            request_id = proposal.request_id
            kind = proposal.kind
            preview = proposal.preview
            payload = proposal.payload
            digest = proposal.digest
        except AttributeError as exc:
            raise TypeError("proposal must expose the frozen action contract") from exc
        created = _now(created_at)
        action = cls(
            action_id=action_id,
            session_id=session_id,
            request_id=request_id,
            kind=kind,
            summary=preview,
            preview=preview,
            payload=payload,
            created_at=created,
            expires_at=created + PENDING_ACTION_TTL,
            presentation_status=presentation_status,
            policy_disposition=policy_disposition,
        )
        if action.digest != digest:
            raise InvariantViolation("proposal digest does not match pending action")
        return action

    def is_expired(self, at: datetime | Clock) -> bool:
        return _now(at) >= self.expires_at

    @property
    def is_confirmable(self) -> bool:
        return self.presentation_status is ProposalPresentationStatus.PRESENTED


@dataclass(frozen=True, slots=True)
class ProposalPresentationFragment:
    """Durable proof of one ordered gateway-accepted proposal fragment."""

    number: int
    total: int
    outbound_id: str
    accepted: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.number, int) or self.number < 1:
            raise ValueError("fragment number must be positive")
        if not isinstance(self.total, int) or self.total < self.number:
            raise ValueError("fragment total must include its number")
        _identifier(self.outbound_id, "outbound_id")
        if self.accepted is not True:
            raise ValueError("only gateway-accepted fragments may be recorded")


class DispatchStatus(str, Enum):
    """Durable lifecycle for one approval-gated dispatch attempt."""

    UNATTEMPTED = "unattempted"
    ATTEMPTED = "attempted"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_STARTED = "not_started"
    CANCELLED = "cancelled"
    CANCELLING = "cancelling"


@dataclass(frozen=True, slots=True)
class ActionDispatchRecord:
    """One durable outbox record; live records retain the exact frozen payload."""

    action_id: str
    session_id: str
    request_id: str
    kind: str
    digest: str
    status: DispatchStatus | str
    approved_at: datetime
    payload: str | None
    preview: str | None
    attempted_at: datetime | None = None
    terminal_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("action_id", "session_id", "request_id", "kind", "digest"):
            _identifier(getattr(self, name), name)
        status = DispatchStatus(self.status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "approved_at", ensure_utc(self.approved_at))
        if self.attempted_at is not None:
            object.__setattr__(self, "attempted_at", ensure_utc(self.attempted_at))
        if self.terminal_at is not None:
            object.__setattr__(self, "terminal_at", ensure_utc(self.terminal_at))
        live = status in {DispatchStatus.UNATTEMPTED, DispatchStatus.ATTEMPTED}
        if live:
            if not isinstance(self.payload, str) or not self.payload:
                raise ValueError("live dispatch records require a frozen payload")
            _identifier(self.preview, "preview")
        elif self.payload is not None or self.preview is not None:
            raise ValueError("terminal dispatch records must remove the frozen payload")
        if status is DispatchStatus.ATTEMPTED and self.attempted_at is None:
            raise ValueError("attempted dispatch records require attempted_at")
        if not live and self.terminal_at is None:
            raise ValueError("terminal dispatch records require terminal_at")

    @classmethod
    def unattempted(
        cls, action: PendingActionState, *, approved_at: datetime | Clock
    ) -> ActionDispatchRecord:
        return cls(
            action_id=action.action_id,
            session_id=action.session_id,
            request_id=action.request_id,
            kind=action.kind,
            digest=action.digest,
            status=DispatchStatus.UNATTEMPTED,
            approved_at=_now(approved_at),
            payload=action.payload,
            preview=action.preview,
        )

    @property
    def is_live(self) -> bool:
        return self.status in {DispatchStatus.UNATTEMPTED, DispatchStatus.ATTEMPTED}

    @property
    def is_cancelling(self) -> bool:
        return self.status is DispatchStatus.CANCELLING

    @property
    def is_open(self) -> bool:
        """Whether the record still needs an external dispatch decision."""

        return self.is_live or self.is_cancelling


def _pending_action_digest(
    *,
    action_id: str,
    request_id: str,
    kind: str,
    preview: str,
    payload: str,
) -> str:
    # Ownership is separately immutable state; this portable content digest is
    # deliberately identical to FrozenActionProposal's presentation digest.
    material = f"{action_id}\x1f{request_id}\x1f{kind}\x1f{preview}\x1f{payload}"
    return sha256(material.encode("utf-8")).hexdigest()
