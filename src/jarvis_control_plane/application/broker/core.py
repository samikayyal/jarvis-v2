# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker core workflow."""

from __future__ import annotations

from .support import *


class _BrokerCoreMixin:
    def __init__(
        self,
        *,
        config: ControlPlaneConfig,
        state: DurableStateStore,
        audit: AuditBoundary,
        orchestration: OrchestrationAdapter,
        outbound: OutboundConnector,
        clock: Clock,
        ids: IdGenerator,
        trace: DiagnosticTraceRecorder,
        model_availability_provider: ModelAvailabilityProvider,
        messaging_readiness_provider: MessagingGatewayReadinessProvider | None = None,
        worker_readiness_provider: WorkerReadinessProvider | None = None,
        google_readiness_provider: ConnectedServiceReadinessProvider | None = None,
        working_sessions: WorkingSessionStore | None = None,
        action_dispatcher: ActionDispatcher | None = None,
        action_lifecycle: BoundActionLifecycle | None = None,
        vault_write_proposal_preparer: KnowledgeVaultWriteProposalPreparer
        | None = None,
    ) -> None:
        if not isinstance(trace, DiagnosticTraceRecorder):
            raise TypeError(
                "trace must be an explicitly configured DiagnosticTraceRecorder"
            )
        self.config = config
        self.state = state
        self.audit = audit
        self.orchestration = orchestration
        self.outbound = outbound
        self.clock = clock
        self.ids = ids
        self.model_availability_provider = model_availability_provider
        self.messaging_readiness_provider = messaging_readiness_provider
        self.worker_readiness_provider = worker_readiness_provider
        self.google_readiness_provider = google_readiness_provider
        self.working_sessions = working_sessions or InMemoryWorkingSessionStore()
        selected_dispatcher = action_dispatcher or _UnavailableActionDispatcher()
        if not isinstance(selected_dispatcher, ActionDispatcher):
            raise TypeError(
                "action_dispatcher must implement the prepared cancellable dispatch port"
            )
        self.action_dispatcher = selected_dispatcher
        self.memory_action_dispatcher = DurableMemoryActionDispatcher(
            state=state,
            clock=clock,
        )
        self.action_lifecycle = action_lifecycle or _NoopActionLifecycle()
        if vault_write_proposal_preparer is not None and not callable(
            getattr(vault_write_proposal_preparer, "propose", None)
        ):
            raise TypeError("vault_write_proposal_preparer must provide propose")
        self.vault_write_proposal_preparer = vault_write_proposal_preparer
        recovery_marker: RecoveryDegradedMarker | None = (
            self.state.load_recovery_degraded_marker()
        )
        self._recovery_degraded = recovery_marker is not None
        self._recovery_degraded_reason = (
            recovery_marker.reason if recovery_marker is not None else None
        )
        existing_session = self.working_sessions.load()
        durable_requests = self.state.list_requests()
        durable_outbound_attempts = (
            self.state.list_outbound_conversation_attempt_recovery()
        )
        durable_restart_state = bool(durable_requests or durable_outbound_attempts)
        if existing_session is not None or durable_restart_state:
            self._reconcile_restart_state(
                existing_session=existing_session,
                requests=durable_requests,
                outbound_attempts=durable_outbound_attempts,
            )
        if existing_session is None:
            self.working_sessions.create(
                WorkingSession.initial(
                    config.operator_id,
                    clock,
                    session_id=(
                        config.working_session_id
                        or f"working-session-{config.session_id}"
                    ),
                    config=SessionConfig(operator_id=config.operator_id),
                )
            )
        if (
            existing_session is not None or durable_restart_state
        ) and not self._recovery_degraded:
            self._record_pending_session_migration()
        # The recorder is a write-only capability backed by an isolated writer.
        # Never retain a readable diagnostic store on the broker graph.
        self._trace = trace
        self._dispatch_lock = RLock()
        self._session_lifecycle_lock = RLock()

    def handle(self, message: InboundMessage) -> ReceiveResult:
        """Accept one already-admitted message and drive its named lifecycle stages."""

        if self._recovery_degraded:
            return ReceiveResult(
                status_code=503,
                disposition="recovery_degraded",
                reason=self._recovery_degraded_reason,
            )
        session = self._reconcile_inactivity()
        readiness_failure = self._refresh_messaging_readiness()
        if readiness_failure is not None:
            return readiness_failure
        readiness_failure = self._refresh_worker_readiness()
        if readiness_failure is not None:
            return readiness_failure
        readiness_failure = self._refresh_google_readiness()
        if readiness_failure is not None:
            return readiness_failure
        session = self._current_working_session()
        if session.pending_action is not None and session.pending_action.is_expired(
            self.clock
        ):
            return self._expire_pending_action(message, session)
        if session.pending_action is not None:
            choice = parse_approval_choice(message.text)
            if choice is not None:
                if not session.pending_action.is_confirmable:
                    return ReceiveResult(
                        status_code=202,
                        disposition="pending_presenting",
                        reason="proposal presentation is incomplete",
                    )
                current = self._current_working_session()
                if current.pending_action is None:
                    return ReceiveResult(
                        status_code=202,
                        disposition="pending_unavailable",
                    )
                if current.pending_action.is_expired(self.clock):
                    return self._expire_pending_action(message, current)
                return self._consume_pending_approval(message, choice)
        memory_command = parse_memory_command(message.text)
        if memory_command is not None:
            return self._handle_memory_command(
                message,
                memory_command,
            )
        parsed = parse_control(message.text)
        if parsed.is_command and parsed.command is ControlCommand.HISTORY:
            if parsed.args and parsed.args[0] == "delete":
                return self._handle_history_deletion_control(message, parsed.args)
            return self._handle_history_control(message, parsed.args)
        if parsed.kind is not MessageKind.ORDINARY:
            try:
                model_availability = self._model_availability()
            except (TypeError, ValueError, RuntimeError) as exc:
                return ReceiveResult(
                    status_code=503,
                    disposition="model_availability_unavailable",
                    reason=f"runtime model availability was unavailable: {exc}",
                )
            session_transition = handle_message(
                session,
                message.text,
                now=self.clock,
                request_id=self.ids.new_id("request"),
                originating_message_id=message.message_id,
                phase="processing",
                model_availability=model_availability,
            )
            return self._handle_session_control(message, session, session_transition)
        admission = self._admit_orchestration_request(
            message=message,
            session=session,
            request_text=message.text,
        )
        if isinstance(admission, ReceiveResult):
            return admission

        request = admission.request
        cancellation_token = admission.cancellation_token
        result = self._run_orchestration(
            message=message,
            request=request,
            cancellation_token=cancellation_token,
        )
        if isinstance(result, ReceiveResult):
            return result
        return self._complete_orchestration_result(
            message=message,
            request=request,
            cancellation_token=cancellation_token,
            result=result,
        )

    def _complete_orchestration_result(
        self,
        *,
        message: InboundMessage,
        request: RequestState,
        cancellation_token: CancellationToken,
        result: OrchestrationResult,
    ) -> ReceiveResult:
        """Apply one orchestration result through the shared broker boundary."""

        if not cancellation_token_is_current(
            self._current_working_session(), cancellation_token
        ):
            return self._late_result_result(request, message=message)

        if not self._selected_configuration_is_available(request):
            return self._configuration_unavailable_result(
                message=message,
                request=request,
                cancellation_token=cancellation_token,
            )

        if result.proposal is not None:
            try:
                action = self.freeze_action(self._bind_action_proposal(result.proposal))
                self._present_action(action, message)
                if action.policy_disposition in {
                    TerminalDisposition.SAFE_READ.value,
                    TerminalDisposition.EXACT_PERMISSION.value,
                }:
                    return self._auto_authorize_terminal_action(action, message)
            except (
                AuditWriteError,
                ActionDispatcherError,
                DiagnosticTraceError,
                InvariantViolation,
                OutboundConnectorError,
                SessionStoreError,
                ValueError,
            ) as exc:
                return self._finish_proposal_failure(
                    message=message,
                    request=request,
                    token=cancellation_token,
                    error=exc,
                )
            return ReceiveResult(
                status_code=202,
                disposition="pending_action",
                request=request,
                reason="exact action proposal is frozen pending operator approval",
            )

        return self._complete_outbound_reply(
            message=message,
            request=request,
            cancellation_token=cancellation_token,
            result=result,
        )

    def _refresh_messaging_readiness(self) -> ReceiveResult | None:
        provider = self.messaging_readiness_provider
        if provider is None:
            return None
        try:
            observation = provider.current()
            ready = observation.messaging_ready is True
        except (RuntimeError, TypeError, ValueError):
            ready = False
        current = self._current_working_session()
        level = "ready" if ready else "unavailable"
        if current.readiness.openwa == level:
            return None
        updated = replace(
            current,
            readiness=replace(current.readiness, openwa=level),
        )
        try:
            self.working_sessions.compare_and_set(current, updated)
        except SessionStoreError:
            return ReceiveResult(
                status_code=503,
                disposition="readiness_state_unavailable",
                reason="messaging-gateway readiness could not be persisted",
            )
        return None

    def _refresh_worker_readiness(self) -> ReceiveResult | None:
        provider = self.worker_readiness_provider
        if provider is None:
            return None
        try:
            observation = provider.current()
            ubuntu = observation.ubuntu
            windows = observation.windows
        except (RuntimeError, TypeError, ValueError):
            ubuntu = "unavailable"
            windows = "unavailable"
        current = self._current_working_session()
        if current.readiness.ubuntu == ubuntu and current.readiness.windows == windows:
            return None
        updated = replace(
            current,
            readiness=replace(
                current.readiness,
                ubuntu=ubuntu,
                windows=windows,
            ),
        )
        try:
            self.working_sessions.compare_and_set(current, updated)
        except SessionStoreError:
            return ReceiveResult(
                status_code=503,
                disposition="readiness_state_unavailable",
                reason="worker readiness could not be persisted",
            )
        return None

    def _refresh_google_readiness(self) -> ReceiveResult | None:
        provider = self.google_readiness_provider
        if provider is None:
            return None
        try:
            observation = provider.current()
            if not isinstance(observation, ServiceReadiness):
                raise TypeError("Google readiness provider returned an invalid value")
            if observation.service_id != "google":
                raise ValueError("Google readiness provider returned the wrong service")
        except Exception:  # noqa: BLE001 - readiness failures become safe unknown state
            observation = ServiceReadiness("google", "unknown")

        current = self._current_working_session()
        services = list(current.readiness.connected_services)
        replaced = False
        for index, service in enumerate(services):
            if service.service_id == "google":
                services[index] = observation
                replaced = True
                break
        if not replaced:
            services.append(observation)
        connected_services = tuple(services)
        if current.readiness.connected_services == connected_services:
            return None
        updated = replace(
            current,
            readiness=replace(
                current.readiness,
                connected_services=connected_services,
            ),
        )
        try:
            self.working_sessions.compare_and_set(current, updated)
        except SessionStoreError:
            return ReceiveResult(
                status_code=503,
                disposition="readiness_state_unavailable",
                reason="Google readiness could not be persisted",
            )
        return None

    @property
    def current_pending_action(self) -> PendingActionState | None:
        """Expose the current frozen action without exposing its payload in status."""

        return self._current_working_session().pending_action

    def _commit_session_with_audit(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        **audit_fields: object,
    ) -> None:
        """Use the session store's shared transaction for state and evidence."""

        evidence = self._audit_evidence(**audit_fields)  # ty:ignore[invalid-argument-type]
        self.working_sessions.compare_and_set_with_audit(
            expected, updated, audit=self.audit, evidence=evidence
        )

    def _record_pending_session_migration(self) -> None:
        """Record and clear a durable permission-shape migration marker."""

        for _ in range(3):
            session = self._current_working_session()
            count = session.legacy_permissions_invalidated
            if count == 0:
                return
            evidence = self._audit_evidence(
                kind="working_session_migration",
                event_id=None,
                request_id=None,
                outcome="migrated",
                actor="control_plane",
                operation_type="working_session",
                target_category="working_session",
                execution_status="recorded",
                details={
                    "count": str(count),
                    "state": "legacy_permissions_invalidated",
                },
            )
            updated = replace(session, legacy_permissions_invalidated=0)
            try:
                self.working_sessions.compare_and_set_with_audit(
                    session, updated, audit=self.audit, evidence=evidence
                )
                return
            except AuditWriteError:
                # Keep the marker durable so a later restart can retry the
                # required migration evidence without restoring authority.
                return
            except SessionStoreError:
                continue
        raise SessionStoreError("could not record working-session migration")
