# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker dispatch lifecycle workflow."""

from __future__ import annotations

from .support import *


class _BrokerDispatchLifecycleMixin:
    @staticmethod
    def _unavailable_terminal_host(
        action: PendingActionState, session: WorkingSession
    ) -> str | None:
        """Return a terminal host that fails the last broker-side readiness gate."""

        if action.kind != "terminal":
            return None
        proposal = FrozenActionProposal(
            action_id=action.action_id,
            request_id=action.request_id,
            kind=action.kind,
            preview=action.preview or "",
            payload=action.payload,
            digest=action.digest,
        )
        host = terminal_action_from_proposal(proposal).host
        readiness = {
            "ubuntu": session.readiness.ubuntu,
            "windows": session.readiness.windows,
        }
        return None if readiness.get(host) == "ready" else host

    def _close_unattempted_action(self, action_id: str) -> None:
        """Fail closed after audit admission fails before the external attempt."""

        current = self._current_working_session()
        try:
            transition = complete_action_dispatch(
                current,
                action_id=action_id,
                status=DispatchStatus.NOT_STARTED,
                now=self.clock,
            )
            self.working_sessions.compare_and_set(current, transition.state)
        except (InvariantViolation, SessionStoreError):
            pass

    def _expire_pending_action(
        self, message: InboundMessage, session: WorkingSession
    ) -> ReceiveResult:
        transition = expire_pending_action(session, now=self.clock)
        try:
            self._commit_session_with_audit(
                session,
                transition.state,
                kind="pending_action",
                event_id=message.event_id,
                request_id=(
                    session.active_request.request_id
                    if session.active_request is not None
                    else None
                ),
                message_id=message.message_id,
                outcome="expired",
                actor="control_plane",
                operation_type="approval_gated_action",
                target_category="pending_action",
                details={"state": "expired"},
            )
        except (AuditWriteError, SessionStoreError) as exc:
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                reason=f"pending expiry was not recorded: {exc}",
            )
        return ReceiveResult(status_code=202, disposition="pending_expired")

    def _finish_proposal_failure(
        self,
        *,
        message: InboundMessage,
        request: RequestState,
        token: CancellationToken,
        error: Exception,
    ) -> ReceiveResult:
        self._close_pending_proposal(token)
        return ReceiveResult(
            status_code=202,
            disposition="failed",
            request=request,
            reason=f"action proposal could not be frozen: {error}",
        )

    def _close_pending_proposal(self, token: CancellationToken) -> None:
        current = self._current_working_session()
        if not cancellation_token_is_current(current, token):
            return
        transition = cancel_active_request(
            current, now=self.clock, reason="proposal_audit_unavailable"
        )
        try:
            self.working_sessions.compare_and_set(current, transition.state)
        except SessionStoreError:
            pass

    def _reconcile_restart_state(
        self,
        *,
        existing_session: WorkingSession | None,
        requests: tuple[RequestState, ...],
        outbound_attempts: tuple[OutboundAttemptRecoveryProjection, ...],
    ) -> None:
        """Admit restart evidence before closing any durable nonterminal edge."""

        terminal_request_statuses = {
            "blocked",
            "cancelled",
            "completed",
            "failed",
            "interrupted",
            "not_started",
            "unknown",
        }
        interrupted = sum(
            request.status not in terminal_request_statuses for request in requests
        )
        open_statuses = {
            OutboundAttemptStatus.UNATTEMPTED.value,
            OutboundAttemptStatus.ATTEMPTED.value,
        }
        terminal_statuses = {
            OutboundAttemptStatus.CONFIRMED.value,
            OutboundAttemptStatus.UNKNOWN.value,
            OutboundAttemptStatus.NOT_STARTED.value,
        }
        inconsistency_counts: Counter[str] = Counter()
        not_started = sum(
            record.status == OutboundAttemptStatus.UNATTEMPTED.value
            for record in outbound_attempts
        )
        unknown = sum(
            record.status == OutboundAttemptStatus.ATTEMPTED.value
            for record in outbound_attempts
        )
        for record in outbound_attempts:
            if not record.attempt_present:
                inconsistency_counts["outbox_without_attempt"] += 1
            elif record.status in open_statuses:
                if not record.outbox_present:
                    inconsistency_counts["open_attempt_without_outbox"] += 1
                elif record.outbox_request_id != record.attempt_request_id:
                    inconsistency_counts["attempt_outbox_request_mismatch"] += 1
            elif record.status not in terminal_statuses:
                inconsistency_counts["unsupported_attempt_status"] += 1
            elif record.outbox_present:
                inconsistency_counts["terminal_attempt_with_outbox"] += 1
        restart_at = self.clock.now()
        session_transition = (
            interrupt_for_restart(existing_session, now=restart_at)
            if existing_session is not None
            else None
        )
        # The injected ID generator can intentionally restart from its first
        # value in reconstructed test and recovery graphs. Restart evidence is
        # process-boundary evidence, so it needs an identity independent of
        # that request-scoped sequence.
        restart_evidence = AuditEvidence(
            evidence_id=f"restart-{uuid.uuid4()}",
            kind="service_restart",
            occurred_at=restart_at,
            event_id=None,
            request_id=None,
            outcome="interrupted",
            actor="control_plane",
            operation_type="working_session",
            target_category="working_session",
            execution_status="recorded",
            details={
                "interrupted_requests": str(interrupted),
                "outbound_not_started": str(not_started),
                "outbound_unknown": str(unknown),
            },
        )
        inconsistency_evidence = tuple(
            AuditEvidence(
                evidence_id=f"restart-inconsistency-{uuid.uuid4()}-{reason}",
                kind="restart_inconsistency",
                occurred_at=restart_at,
                event_id=None,
                request_id=None,
                outcome="degraded",
                actor="control_plane",
                operation_type="state_recovery",
                target_category="durable_state",
                execution_status="recorded",
                details={
                    "count": str(count),
                    "reason": reason,
                    "state": "administrative_degraded",
                },
            )
            for reason, count in sorted(inconsistency_counts.items())
        )
        # Admit the required restart evidence before closing any durable
        # nonterminal edge. When a working session exists, use its
        # state-plus-audit compare-and-set so the session cannot be changed
        # without this evidence. A missing session has no state transition to
        # atomically join, so audit admission remains the gate before session
        # creation in the constructor.
        if session_transition is None:
            self.audit.append_batch((restart_evidence, *inconsistency_evidence))
        else:
            # A session CAS can atomically carry one audit record.  Admit any
            # additional bounded inconsistency evidence first; no request or
            # outbound state is changed until both evidence paths succeed.
            if inconsistency_evidence:
                self.audit.append_batch(inconsistency_evidence)
        if inconsistency_counts:
            reason = (
                "restart found inconsistent durable outbound state; "
                "administrative repair is required"
            )
            self.state.mark_recovery_degraded(
                reason=reason,
                marked_at=restart_at,
            )
            self._recovery_degraded = True
            self._recovery_degraded_reason = reason
        if session_transition is not None:
            self.working_sessions.compare_and_set_with_audit(
                existing_session,
                session_transition.state,
                audit=self.audit,
                evidence=restart_evidence,
            )
        for request in requests:
            if request.status in terminal_request_statuses:
                continue
            self.state.update_request(
                replace(
                    request,
                    updated_at=restart_at,
                    status="interrupted",
                    phase="interrupted",
                    outcome="interrupted",
                    error_code="service_restart",
                )
            )
        self.state.reconcile_outbound_conversation_attempts(interrupted_at=restart_at)
