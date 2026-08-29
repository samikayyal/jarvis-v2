# ruff: noqa: F401, I001, RUF100 -- mixin globals preserve compatibility seams.
"""In-memory durable state adapter."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime

from ..conversation_archive import InMemoryDeletedConversationArchive
from ..models import (
    AuditEvidence,
    ConversationDeletionPreview,
    ConversationDeletionScope,
    ConversationMessage,
    ConversationTombstone,
    DurableMemory,
    HistorySelection,
    IngressAdmissionResult,
    IngressClaim,
    MemoryLifecycle,
    MemorySelection,
    OutboundAttemptRecord,
    OutboundAttemptRecoveryProjection,
    OutboundAttemptStatus,
    RecoveryDegradedMarker,
    RequestState,
    ensure_utc,
    is_outbound_terminal_transition_allowed,
)
from ..ports import (
    AuditBoundary,
    AuditWriteError,
    DeletedConversationArchiveError,
    DeletedConversationArchiveWriter,
    StateStoreError,
)
from .state_support import (
    _abort_deleted_archive,
    _conversation_tombstone,
    _export_conversation_messages,
    _filter_conversation_messages,
    _filter_memories,
    _finalize_deleted_archive,
    _locked_durable_state,
    _preview_conversation_deletion,
    _select_history_for_context,
    _select_memories_for_context,
    _stage_deleted_archive,
)


class _InMemoryIngressMixin:
    @_locked_durable_state
    def load_recovery_degraded_marker(self) -> RecoveryDegradedMarker | None:
        with self._lock:
            return self._recovery_degraded_marker

    @_locked_durable_state
    def mark_recovery_degraded(self, *, reason: str, marked_at: datetime) -> None:
        marker = RecoveryDegradedMarker(reason=reason, marked_at=marked_at)
        with self._lock:
            if self._recovery_degraded_marker is None:
                self._recovery_degraded_marker = marker

    @_locked_durable_state
    def acknowledge_recovery_degraded(self) -> None:
        """Clear the marker only when called by an explicit admin flow."""

        with self._lock:
            self._recovery_degraded_marker = None

    @_locked_durable_state
    def admit_ingress(
        self,
        *,
        session_id: str,
        message_id: str,
        event_id: str,
        claimed_at: datetime,
        conversation_message: ConversationMessage | None,
        audit: AuditBoundary,
        audit_evidence: AuditEvidence,
        terminal_disposition: str,
        audit_blocked_disposition: str | None = None,
    ) -> IngressAdmissionResult:
        """Atomically admit one keyed event with a terminal disposition.

        The in-memory adapter stages the state write only after the required
        audit append succeeds.  An admitted operator message may instead be
        retained as ``audit_blocked`` when audit is unavailable; rejected
        traffic is safely discarded in that case because it creates no work or
        conversation history.
        """

        with self._lock:
            key = (session_id, message_id)
            if key in self.claims:
                return IngressAdmissionResult(
                    claimed=False,
                    disposition="duplicate",
                )
            if self.fail_claim:
                raise StateStoreError("controlled ingress claim failure")
            if conversation_message is not None:
                if self.fail_conversation:
                    raise StateStoreError("controlled conversation write failure")
                if (
                    conversation_message.transport_session_id,
                    conversation_message.message_id,
                ) != key:
                    raise StateStoreError(
                        "conversation message key does not match claim"
                    )
            if self.fail_update:
                raise StateStoreError("controlled ingress disposition update failure")

            try:
                audit.append(audit_evidence)
            except AuditWriteError:
                if audit_blocked_disposition is None:
                    return IngressAdmissionResult(
                        claimed=False,
                        disposition=terminal_disposition,
                    )
                disposition = audit_blocked_disposition
            else:
                disposition = terminal_disposition

            self.claims[key] = IngressClaim(
                session_id=session_id,
                message_id=message_id,
                event_id=event_id,
                claimed_at=claimed_at,
                disposition=disposition,
            )
            if conversation_message is not None:
                self.conversation_messages[key] = conversation_message
            return IngressAdmissionResult(
                claimed=True,
                disposition=disposition,
            )

    @_locked_durable_state
    def claim_ingress(
        self,
        *,
        session_id: str,
        message_id: str,
        event_id: str,
        claimed_at: datetime,
        conversation_message: ConversationMessage | None = None,
        disposition: str = "admitted",
    ) -> bool:
        with self._lock:
            if disposition == "pending_audit":
                raise StateStoreError("ingress claims require a terminal disposition")
            if self.fail_claim:
                raise StateStoreError("controlled ingress claim failure")
            key = (session_id, message_id)
            if key in self.claims:
                return False
            if conversation_message is not None:
                if self.fail_conversation:
                    raise StateStoreError("controlled conversation write failure")
                if (
                    conversation_message.transport_session_id,
                    conversation_message.message_id,
                ) != key:
                    raise StateStoreError(
                        "conversation message key does not match claim"
                    )
                self.conversation_messages[key] = conversation_message
            self.claims[key] = IngressClaim(
                session_id=session_id,
                message_id=message_id,
                event_id=event_id,
                claimed_at=claimed_at,
                disposition=disposition,
            )
            return True

    @_locked_durable_state
    def update_ingress_disposition(
        self,
        *,
        session_id: str,
        message_id: str,
        disposition: str,
    ) -> None:
        with self._lock:
            if disposition == "pending_audit":
                raise StateStoreError("ingress claims require a terminal disposition")
            if self.fail_update:
                raise StateStoreError("controlled ingress disposition update failure")
            key = (session_id, message_id)
            claim = self.claims.get(key)
            if claim is None:
                raise StateStoreError("ingress claim does not exist")
            self.claims[key] = IngressClaim(
                session_id=claim.session_id,
                message_id=claim.message_id,
                event_id=claim.event_id,
                claimed_at=claim.claimed_at,
                disposition=disposition,
            )

    @_locked_durable_state
    def begin_next_ingress_dispatch(self) -> ConversationMessage | None:
        with self._lock:
            pending = sorted(
                (
                    claim
                    for claim in self.claims.values()
                    if claim.disposition == "admitted"
                ),
                key=lambda claim: (
                    claim.claimed_at,
                    claim.session_id,
                    claim.message_id,
                ),
            )
            for claim in pending:
                key = (claim.session_id, claim.message_id)
                message = self.conversation_messages.get(key)
                if message is None:
                    continue
                self.claims[key] = replace(claim, disposition="dispatching")
                return message
            return None

    @_locked_durable_state
    def begin_ingress_dispatch(
        self, *, transport_session_id: str, message_id: str
    ) -> bool:
        with self._lock:
            key = (transport_session_id, message_id)
            claim = self.claims.get(key)
            if (
                claim is None
                or claim.disposition != "admitted"
                or key not in self.conversation_messages
            ):
                return False
            self.claims[key] = replace(claim, disposition="dispatching")
            return True

    @_locked_durable_state
    def finish_ingress_dispatch(
        self,
        *,
        transport_session_id: str,
        message_id: str,
        disposition: str,
    ) -> None:
        if disposition not in {"dispatched", "interrupted"}:
            raise StateStoreError("ingress dispatch disposition is invalid")
        with self._lock:
            key = (transport_session_id, message_id)
            claim = self.claims.get(key)
            if claim is None or claim.disposition != "dispatching":
                raise StateStoreError("ingress dispatch is not active")
            self.claims[key] = replace(claim, disposition=disposition)

    @_locked_durable_state
    def reconcile_ingress_restart(
        self,
        *,
        audit: AuditBoundary,
        audit_evidence: AuditEvidence,
    ) -> int:
        """Atomically audit and interrupt all nonterminal ingress work."""

        with self._lock:
            nonterminal = tuple(
                (key, claim)
                for key, claim in self.claims.items()
                if claim.disposition in {"admitted", "dispatching"}
            )
            if not nonterminal:
                return 0
            audit.append(audit_evidence)
            for key, claim in nonterminal:
                self.claims[key] = replace(claim, disposition="interrupted")
            return len(nonterminal)
