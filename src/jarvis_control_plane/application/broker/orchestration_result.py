# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker orchestration result workflow."""

from __future__ import annotations

from .support import *


class _BrokerOrchestrationResultMixin:
    def _prepare_orchestration_proposal(
        self, result: OrchestrationResult, *, request_id: str
    ) -> OrchestrationResult:
        """Turn a model intent into an exact action only at the broker boundary."""

        intent = result.proposal_intent
        if intent is None:
            return result
        if not isinstance(intent, OrchestrationProposalIntent):
            raise OrchestrationAdapterError(
                "orchestration adapter returned an invalid proposal intent"
            )
        if intent.kind != "knowledge_vault_write":
            raise OrchestrationAdapterError(
                "orchestration proposal intent is outside the broker boundary"
            )
        preparer = self.vault_write_proposal_preparer
        if preparer is None:
            raise OrchestrationAdapterError(
                "knowledge-vault write capability is not configured"
            )
        if set(intent.payload) != {"changes"}:
            raise OrchestrationAdapterError(
                "knowledge-vault write proposal intent has an unexpected shape"
            )
        changes = intent.payload.get("changes")
        if not isinstance(changes, Mapping) or any(
            not isinstance(path, str) or not isinstance(content, str)
            for path, content in changes.items()
        ):
            raise OrchestrationAdapterError(
                "knowledge-vault write proposal intent has an unexpected shape"
            )

        def prepare() -> OrchestrationResult:
            try:
                action = preparer.propose(request_id=request_id, changes=changes)
            except OrchestrationAdapterError:
                raise
            except Exception as exc:
                raise OrchestrationAdapterError(
                    "knowledge-vault proposal preparation failed"
                ) from exc
            if not isinstance(action, FrozenActionProposal):
                raise OrchestrationAdapterError(
                    "knowledge-vault proposal preparer returned an invalid action"
                )
            if action.request_id != request_id or action.kind != intent.kind:
                raise OrchestrationAdapterError(
                    "knowledge-vault proposal preparer returned a mismatched action"
                )
            return replace(result, proposal=action, proposal_intent=None)

        return self._trace.execute(
            request_id=request_id,
            operation_id=f"{request_id}:vault-proposal",
            operation_type="connector_proposal_preparation",
            input_payload=intent,
            arguments={"kind": intent.kind},
            telemetry={"phase": "connector_proposal_preparation"},
            operation=prepare,
            result_limit_bytes=self.config.max_text_length * 8 + 4_096,
            error_limit_bytes=8_192,
        )

    @staticmethod
    def _validate_orchestration_result(
        result: object, *, request_id: str
    ) -> str | None:
        """Validate model output before it can reach policy or outbound edges."""

        if not isinstance(result, OrchestrationResult):
            raise OrchestrationAdapterError(
                "orchestration adapter returned an untyped result"
            )
        if result.request_id != request_id:
            raise OrchestrationAdapterError("orchestration result correlation mismatch")
        if result.outcome not in {"completed", "unavailable"}:
            raise OrchestrationAdapterError(
                "orchestration adapter returned an unsupported outcome"
            )
        if result.adapter not in {"controlled", "agents_sdk_responses"}:
            raise OrchestrationAdapterError(
                "orchestration adapter is outside the configured boundary"
            )
        if result.proposal is not None and not isinstance(
            result.proposal, FrozenActionProposal
        ):
            raise OrchestrationAdapterError(
                "orchestration adapter returned an untyped proposal"
            )
        if result.proposal_intent is not None:
            if not isinstance(result.proposal_intent, OrchestrationProposalIntent):
                raise OrchestrationAdapterError(
                    "orchestration adapter returned an invalid proposal intent"
                )
            if result.proposal_intent.request_id != request_id:
                raise OrchestrationAdapterError(
                    "orchestration proposal intent correlation mismatch"
                )
            if result.proposal_intent.kind != "knowledge_vault_write":
                raise OrchestrationAdapterError(
                    "orchestration proposal intent is outside the configured boundary"
                )
        if result.outcome == "unavailable" and any(
            value is not None
            for value in (
                result.proposal,
                result.proposal_intent,
                result.execution_host,
                result.host_reason_code,
            )
        ):
            raise OrchestrationAdapterError(
                "unavailable orchestration result included action authority"
            )
        selected_host = result.execution_host
        if result.proposal is None or result.proposal.kind != "terminal":
            return selected_host
        try:
            terminal = terminal_action_from_proposal(result.proposal)
        except (TypeError, ValueError) as exc:
            raise OrchestrationAdapterError(
                "orchestration adapter returned a malformed terminal proposal"
            ) from exc
        if terminal.host not in {"ubuntu", "windows"}:
            raise OrchestrationAdapterError(
                "terminal proposal selected an unknown execution host"
            )
        if selected_host is not None and terminal.host != selected_host:
            raise OrchestrationAdapterError(
                "terminal proposal host does not match host selection"
            )
        return terminal.host

    def _record_execution_host(
        self,
        request: RequestState,
        token: CancellationToken,
        selected_host: str,
    ) -> None:
        """Persist the planner's closed host selection on the live request."""

        if selected_host not in {"ubuntu", "windows"}:
            raise OrchestrationAdapterError(
                "orchestration selected an unknown execution host"
            )
        for _ in range(3):
            current = self._current_working_session()
            if not cancellation_token_is_current(current, token):
                raise OrchestrationAdapterError(
                    "orchestration result no longer owns the working session"
                )
            active = current.active_request
            if active is None or active.request_id != request.request_id:
                raise OrchestrationAdapterError(
                    "orchestration result has no matching active request"
                )
            if active.execution_host is not None:
                if active.execution_host != selected_host:
                    raise OrchestrationAdapterError(
                        "execution host changed during orchestration"
                    )
                return
            updated = replace(
                current,
                active_request=replace(
                    active,
                    execution_host=selected_host,
                    updated_at=self.clock.now(),
                ),
            )
            try:
                self.working_sessions.compare_and_set(current, updated)
                return
            except SessionStoreError:
                continue
        raise SessionStoreError("execution host selection raced the working session")

    def _configuration_unavailable_result(
        self,
        *,
        message: InboundMessage,
        request: RequestState,
        cancellation_token: CancellationToken,
    ) -> ReceiveResult:
        """Finish the request without substituting an unavailable configuration."""

        self._finish_session_request(
            cancellation_token,
            outcome="model_availability_unavailable",
            message=message,
        )
        failed = self._transition(
            request,
            status="failed",
            phase="orchestration",
            outcome="orchestration_failed",
            error_code="model_availability_unavailable",
        )
        return ReceiveResult(
            status_code=202,
            disposition="model_availability_unavailable",
            request=failed,
            reason="selected model or reasoning became unavailable before dispatch",
        )
