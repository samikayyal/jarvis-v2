"""Redacted append-only audit evidence and deterministic safe filters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .audit_rules import _validate_audit_event_semantics
from .audit_values import (
    _LEGACY_EXECUTION_STATUSES,
    _LEGACY_OPERATION_TYPES,
    _LEGACY_TARGET_CATEGORIES,
    _MAX_AUDIT_VIEW_LIMIT,
    _audit_identifier,
    _normalize_audit_details,
)
from .ingress_messaging import ensure_utc


@dataclass(frozen=True, slots=True)
class AuditEvidence:
    """Redacted append-only evidence; raw transport content never belongs here."""

    evidence_id: str
    kind: str
    occurred_at: datetime
    event_id: str | None = None
    request_id: str | None = None
    outcome: str = "recorded"
    actor: str = "control_plane"
    details: Mapping[str, str] = field(default_factory=dict)
    redacted: bool = True
    message_id: str | None = None
    operation_type: str | None = None
    target_category: str | None = None
    approval_decision: str | None = None
    policy_decision: str | None = None
    execution_status: str | None = None

    def __post_init__(self) -> None:
        _audit_identifier(self.evidence_id, "evidence_id")
        _audit_identifier(self.kind, "kind")
        _audit_identifier(self.outcome, "outcome")
        _audit_identifier(self.actor, "actor")
        if self.event_id is not None:
            _audit_identifier(self.event_id, "event_id")
        if self.request_id is not None:
            _audit_identifier(self.request_id, "request_id")
        for name in (
            "message_id",
            "operation_type",
            "target_category",
            "approval_decision",
            "policy_decision",
            "execution_status",
        ):
            value = getattr(self, name)
            if value is not None:
                _audit_identifier(value, name)
        if self.redacted is not True:
            raise ValueError("audit evidence must be redacted")
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(
            self,
            "details",
            _normalize_audit_details(self.details, kind=self.kind),
        )
        _validate_audit_event_semantics(
            kind=self.kind,
            operation_type=self.operation_type,
            target_category=self.target_category,
            actor=self.actor,
            outcome=self.outcome,
            execution_status=self.execution_status,
            details=self.details,
        )

    @property
    def operation(self) -> str | None:
        """Compatibility alias for callers that call the operation a category."""

        return self.operation_type or _LEGACY_OPERATION_TYPES.get(self.kind, self.kind)

    @property
    def effective_target_category(self) -> str | None:
        return self.target_category or _LEGACY_TARGET_CATEGORIES.get(self.actor)

    @property
    def effective_execution_status(self) -> str | None:
        return self.execution_status or _LEGACY_EXECUTION_STATUSES.get(self.outcome)

    @property
    def approval(self) -> str | None:
        """Compatibility alias for the safe approval decision field."""

        return self.approval_decision

    @property
    def policy(self) -> str | None:
        """Compatibility alias for the safe policy decision field."""

        return self.policy_decision

    def as_safe_mapping(self) -> dict[str, Any]:
        """Return the only representation that may cross the admin export boundary."""

        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "occurred_at": self.occurred_at.isoformat(),
            "event_id": self.event_id,
            "request_id": self.request_id,
            "message_id": self.message_id,
            "operation_type": self.operation,
            "target_category": self.effective_target_category,
            "approval_decision": self.approval_decision,
            "policy_decision": self.policy_decision,
            "execution_status": self.effective_execution_status,
            "outcome": self.outcome,
            "actor": self.actor,
            "details": dict(self.details),
            "redacted": True,
        }


@dataclass(frozen=True, slots=True)
class AuditFilter:
    """Deterministic filters for the bounded redacted audit view.

    ``start_at`` is inclusive and ``end_at`` is exclusive.  ``on_date`` is a
    UTC calendar-day shortcut and cannot be combined with a time range.
    ``limit`` bounds the returned view rather than changing stored evidence.
    When omitted, the safe administration surface still applies the maximum
    bounded page size.
    """

    start_at: datetime | None = None
    end_at: datetime | None = None
    on_date: date | None = None
    request_id: str | None = None
    operation_type: str | None = None
    target_category: str | None = None
    approval_decision: str | None = None
    policy_decision: str | None = None
    execution_status: str | None = None
    outcome: str | None = None
    limit: int = _MAX_AUDIT_VIEW_LIMIT

    def __post_init__(self) -> None:
        normalized_start = ensure_utc(self.start_at) if self.start_at else None
        normalized_end = ensure_utc(self.end_at) if self.end_at else None
        if normalized_start and normalized_end and normalized_start > normalized_end:
            raise ValueError("audit filter start_at must not be after end_at")
        if self.on_date is not None:
            if not isinstance(self.on_date, date) or isinstance(self.on_date, datetime):
                raise TypeError("audit filter on_date must be a calendar date")
            if normalized_start is not None or normalized_end is not None:
                raise ValueError(
                    "audit filter on_date cannot be combined with a time range"
                )
        if (
            not isinstance(self.limit, int)
            or isinstance(self.limit, bool)
            or self.limit <= 0
            or self.limit > _MAX_AUDIT_VIEW_LIMIT
        ):
            raise ValueError(
                "audit filter limit must be a positive integer no greater than 1000"
            )
        for name in (
            "request_id",
            "operation_type",
            "target_category",
            "approval_decision",
            "policy_decision",
            "execution_status",
            "outcome",
        ):
            value = getattr(self, name)
            if value is not None:
                _audit_identifier(value, name)
        object.__setattr__(self, "start_at", normalized_start)
        object.__setattr__(self, "end_at", normalized_end)

    def matches(self, evidence: AuditEvidence) -> bool:
        """Return whether one redacted record belongs in this safe view."""

        occurred_at = ensure_utc(evidence.occurred_at)
        if self.on_date is not None and occurred_at.date() != self.on_date:
            return False
        if self.start_at is not None and occurred_at < self.start_at:
            return False
        if self.end_at is not None and occurred_at >= self.end_at:
            return False
        if self.request_id is not None and evidence.request_id != self.request_id:
            return False
        if (
            self.operation_type is not None
            and evidence.operation != self.operation_type
        ):
            return False
        if (
            self.target_category is not None
            and evidence.effective_target_category != self.target_category
        ):
            return False
        if (
            self.approval_decision is not None
            and evidence.approval_decision != self.approval_decision
        ):
            return False
        if (
            self.policy_decision is not None
            and evidence.policy_decision != self.policy_decision
        ):
            return False
        if (
            self.execution_status is not None
            and evidence.effective_execution_status != self.execution_status
        ):
            return False
        return not (self.outcome is not None and evidence.outcome != self.outcome)
