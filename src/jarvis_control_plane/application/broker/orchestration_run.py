# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker orchestration run workflow."""

from __future__ import annotations

from .support import *


class _BrokerOrchestrationRunMixin:
    def _admit_request(
        self,
        *,
        message: InboundMessage,
        session: WorkingSession,
        session_transition: ControlTransition,
        request_id: str,
    ) -> _RequestAdmission | ReceiveResult:
        """Persist one request and claim the matching working-session generation."""

        request = RequestState(
            request_id=request_id,
            event_id=message.event_id,
            message_id=message.message_id,
            operator_id=self.config.operator_id,
            session_id=self.config.session_id,
            chat_id=message.chat_id,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
            status="accepted",
            phase="orchestration",
            model=session.model,
            reasoning=session.reasoning,
        )
        try:
            self.state.save_request(request)
            self._append_audit(
                kind="request_accepted",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome="accepted",
                actor="configured_operator",
                operation_type="request_lifecycle",
                target_category="control_plane",
                details={
                    "phase": "orchestration",
                    "model": request.model,
                    "reasoning": request.reasoning,
                },
            )
        except (StateStoreError, AuditWriteError) as admission_error:
            try:
                self.state.delete_request(request.request_id)
            except StateStoreError as transition_error:
                return ReceiveResult(
                    status_code=202,
                    disposition="failed",
                    request=request,
                    reason=(
                        "required audit evidence was unavailable and the blocked state "
                        "could not be rolled back: "
                        f"{transition_error}"
                    ),
                )
            blocked = replace(
                request,
                status="blocked",
                phase="audit_gate",
                outcome="audit_unavailable",
                error_code="audit_unavailable",
            )
            return ReceiveResult(
                status_code=202,
                disposition="audit_blocked",
                request=blocked,
                reason=(
                    "required audit evidence was unavailable; the request admission "
                    f"was rolled back: {admission_error}"
                ),
            )

        try:
            self.working_sessions.compare_and_set(session, session_transition.state)
        except SessionStoreError as exc:
            try:
                self.state.delete_request(request.request_id)
            except StateStoreError:
                pass
            self._best_effort_audit(
                kind="working_session_conflict",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome="blocked",
                actor="control_plane",
                operation_type="request_lifecycle",
                target_category="working_session",
                details={},
            )
            return ReceiveResult(
                status_code=202,
                disposition="failed",
                request=request,
                reason=f"working-session admission failed: {exc}",
            )

        cancellation_token = session_transition.cancellation_token
        assert cancellation_token is not None

        return _RequestAdmission(
            request=request,
            cancellation_token=cancellation_token,
        )

    def _admit_orchestration_request(
        self,
        *,
        message: InboundMessage,
        session: WorkingSession,
        request_text: str,
    ) -> _RequestAdmission | ReceiveResult:
        """Apply shared model availability and request admission policy."""

        try:
            model_availability = self._model_availability()
        except (TypeError, ValueError, RuntimeError) as exc:
            return ReceiveResult(
                status_code=503,
                disposition="model_availability_unavailable",
                reason=f"runtime model availability was unavailable: {exc}",
            )

        request_id = self.ids.new_id("request")
        session_transition = admit_orchestration_request(
            session,
            request_text,
            now=self.clock,
            request_id=request_id,
            originating_message_id=message.message_id,
            phase="processing",
            model_availability=model_availability,
        )
        if session_transition.kind is not ControlTransitionKind.REQUEST_ACCEPTED:
            return self._handle_session_control(message, session, session_transition)
        return self._admit_request(
            message=message,
            session=session,
            session_transition=session_transition,
            request_id=request_id,
        )

    def _run_orchestration(
        self,
        *,
        message: InboundMessage,
        request: RequestState,
        cancellation_token: CancellationToken,
        orchestration_text: str | None = None,
        memory_selection: MemorySelection | None = None,
    ) -> OrchestrationResult | ReceiveResult:
        """Execute and durably observe the controlled orchestration stage."""

        try:
            prompt_text = orchestration_text or message.text
            history = self.state.select_history_for_context(
                text=prompt_text,
                excluding_working_session_id=self.current_working_session_id,
                limit=5,
            )
            memories = memory_selection or self.state.select_memories_for_context(
                text=prompt_text,
                limit=5,
            )
            orchestration_request = OrchestrationRequest(
                state=request,
                text=prompt_text,
                history=history.messages,
                memories=memories.memories,
                memory_selection=memories,
            )
            result = self._trace.execute(
                request_id=request.request_id,
                operation_id=f"{request.request_id}:model",
                operation_type="model",
                input_payload=orchestration_request,
                arguments={
                    "adapter": type(self.orchestration).__name__,
                    "operation": "run",
                    "model": request.model,
                    "reasoning": request.reasoning,
                },
                telemetry={
                    "phase": "orchestration",
                    "model": request.model,
                    "reasoning": request.reasoning,
                },
                operation=lambda: self.orchestration.run(orchestration_request),
                result_limit_bytes=self.config.max_text_length * 8 + 4_096,
                error_limit_bytes=8_192,
            )
            selected_host = self._validate_orchestration_result(
                result, request_id=request.request_id
            )
            result = self._prepare_orchestration_proposal(
                result, request_id=request.request_id
            )
            if selected_host is not None:
                self._record_execution_host(request, cancellation_token, selected_host)
            self._append_audit(
                kind="orchestration_result",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome=result.outcome,
                actor="controlled_orchestration",
                operation_type="orchestration",
                target_category="control_plane",
                details={
                    "adapter": result.adapter,
                    "model": request.model,
                    "reasoning": request.reasoning,
                },
            )
            if history.provenance_disclosure is not None:
                result = replace(
                    result,
                    reply_text=f"{history.provenance_disclosure}\n{result.reply_text}",
                )
            if memories.provenance_disclosure is not None:
                result = replace(
                    result,
                    reply_text=f"{memories.provenance_disclosure}\n{result.reply_text}",
                )
        except (
            DiagnosticTraceError,
            OrchestrationAdapterError,
            AuditWriteError,
            SessionStoreError,
            StateStoreError,
            ValueError,
        ) as exc:
            trace_failed = isinstance(exc, (TraceCapacityError, TraceWriteError))
            failure_outcome = (
                "trace_unavailable" if trace_failed else "orchestration_failed"
            )
            failure_code = (
                "trace_capacity"
                if isinstance(exc, TraceCapacityError)
                else (
                    "trace_error"
                    if isinstance(exc, TraceWriteError)
                    else "orchestration_error"
                )
            )
            try:
                failed = self._transition(
                    request,
                    status="failed",
                    phase="orchestration",
                    outcome=failure_outcome,
                    error_code=failure_code,
                )
            except (StateStoreError, AuditWriteError):
                self._best_effort_audit(
                    kind="orchestration_result",
                    event_id=message.event_id,
                    request_id=request.request_id,
                    message_id=message.message_id,
                    outcome="failed",
                    actor="controlled_orchestration",
                    operation_type="orchestration",
                    target_category="control_plane",
                    details={"result": "failed", "state": "unavailable"},
                )
                self._finish_session_request(
                    cancellation_token,
                    outcome=failure_outcome,
                    message=message,
                )
                return self._dispatch_orchestration_failure(
                    message=message,
                    request=request,
                    failure_outcome=(
                        f"{failure_outcome}; the failure state could not be persisted"
                    ),
                )
            self._best_effort_audit(
                kind="orchestration_result",
                event_id=message.event_id,
                request_id=request.request_id,
                message_id=message.message_id,
                outcome="failed",
                actor="controlled_orchestration",
                operation_type="orchestration",
                target_category="control_plane",
                details={"result": "failed"},
            )
            self._finish_session_request(
                cancellation_token,
                outcome=failure_outcome,
                message=message,
            )
            return self._dispatch_orchestration_failure(
                message=message,
                request=failed,
                failure_outcome=failure_outcome,
            )

        return result

    def _dispatch_orchestration_failure(
        self,
        *,
        message: InboundMessage,
        request: RequestState,
        failure_outcome: str,
    ) -> ReceiveResult:
        """Send one bounded failure notice after the request is terminalized."""

        control_id = self.ids.new_id("orchestration-failure")
        body = _bounded_informational_reply(
            "I could not complete that request because the orchestration service "
            "failed. No action was taken.",
            request_id=control_id,
        )
        response = self._dispatch_control_text(
            message,
            body=body,
            control_id=control_id,
            disposition="failed",
        )
        if response.disposition == "unknown":
            reason = (
                f"{failure_outcome}; the failure response has an unknown outbound "
                "outcome and will not be retried"
            )
        elif response.disposition == "failed":
            reason = f"{failure_outcome}; the failure response was not sent"
        else:
            reason = failure_outcome
        return replace(response, request=request, reason=reason)
