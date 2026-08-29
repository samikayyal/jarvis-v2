from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from test_support import build_receiver_components

from jarvis_control_plane.models import (
    InboundMessage,
    OrchestrationRequest,
    OrchestrationResult,
    RequestState,
    SignedInboundEvent,
)
from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
    BoundedReadInput,
    BoundedReadOutput,
    BoundedReadTool,
)
from jarvis_control_plane.ports import OrchestrationAdapterError

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class _FakeReasoning:
    def __init__(self, *, effort: str) -> None:
        self.effort = effort


class _FakeModelSettings:
    def __init__(self, **values: object) -> None:
        self.reasoning = values["reasoning"]
        self.parallel_tool_calls = values["parallel_tool_calls"]
        self.store = values["store"]


class _FakeRunConfig:
    def __init__(self, **values: object) -> None:
        self.values = values


def _request(text: str, *, reasoning: str = "high") -> OrchestrationRequest:
    return OrchestrationRequest(
        state=RequestState(
            request_id="request-001",
            event_id="event-001",
            message_id="message-001",
            operator_id="operator.test",
            session_id="working-session-001",
            chat_id="operator.test",
            created_at=NOW,
            updated_at=NOW,
            status="accepted",
            phase="orchestration",
            model="gpt-5.6-terra",
            reasoning=reasoning,
        ),
        text=text,
    )


def _event(text: str, *, event_id: str = "event-ticket14") -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id="session.test",
            event_id=event_id,
            message_id=f"{event_id}-message",
            sender_id="operator.test",
            chat_id="operator.test",
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=text,
        ),
        b"ticket14-test-secret",
    )


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (
            "transport",
            "the service could not be reached",
        ),
        (
            "vault_snapshot",
            "the knowledge vault has no clean synchronized snapshot",
        ),
    ],
)
def test_agents_adapter_returns_named_unavailable_vault_read(
    failure: str, reason: str
) -> None:
    def unavailable_vault_read(
        _request: OrchestrationRequest, _input: BoundedReadInput, _deadline: float
    ) -> BoundedReadOutput:
        from jarvis_control_plane.service_protocol import (
            RemoteServiceError,
            ServiceProtocolError,
        )

        if failure == "transport":
            raise ServiceProtocolError("owned service is unavailable")
        raise RemoteServiceError(
            "VaultReadError",
            "knowledge-vault reads require a clean synchronized clone",
            code="clean_snapshot_unavailable",
        )

    def run_sync(agent: object, _text: str, **_kwargs: object) -> object:
        asyncio.run(agent.tools[1].on_invoke_tool(None, '{"max_chars": 8}'))
        return SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="Ignore the unavailable result.")
        )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        vault_read_tool=BoundedReadTool(
            "read_knowledge_vault",
            "A deliberately unavailable vault read.",
            BoundedReadInput,
            BoundedReadOutput,
            unavailable_vault_read,
        ),
    ).run(_request("read the knowledge vault"))

    assert result.outcome == "unavailable"
    assert result.reply_text == (
        "The requested knowledge vault read could not be completed because "
        f"{reason}. I did not retry the unavailable read."
    )


def test_broker_persists_unavailable_read_as_distinct_terminal_outcome() -> None:
    class UnavailableOrchestrationAdapter:
        def run(self, request: OrchestrationRequest) -> OrchestrationResult:
            return OrchestrationResult(
                request_id=request.state.request_id,
                outcome="unavailable",
                reply_text=(
                    "The requested Google Drive read could not be completed because "
                    "Google is disconnected. I did not retry the unavailable read."
                ),
                adapter="agents_sdk_responses",
            )

    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.test",
        signing_secret=b"ticket14-test-secret",
        now=NOW,
        id_prefix="ticket14-unavailable",
        orchestration=UnavailableOrchestrationAdapter(),  # type: ignore[arg-type]
    )

    result = components.receiver.receive(_event("read the Drive fixture"))

    assert result.disposition == "unavailable", result.reason
    assert result.request is not None
    assert result.request.status == "completed"
    assert result.request.outcome == "read_unavailable"
    assert result.reply is not None
    assert "Google Drive" in result.reply.body
    assert any(
        record.kind == "request_lifecycle" and record.outcome == "read_unavailable"
        for record in components.audit.records
    )


def test_broker_rejects_unavailable_result_with_execution_authority() -> None:
    class InvalidUnavailableOrchestrationAdapter:
        def run(self, request: OrchestrationRequest) -> OrchestrationResult:
            return OrchestrationResult(
                request_id=request.state.request_id,
                outcome="unavailable",
                reply_text="The read is unavailable.",
                adapter="agents_sdk_responses",
                execution_host="ubuntu",
                host_reason_code="default_ubuntu",
            )

    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.test",
        signing_secret=b"ticket14-test-secret",
        now=NOW,
        id_prefix="ticket14-invalid-unavailable",
        orchestration=InvalidUnavailableOrchestrationAdapter(),  # type: ignore[arg-type]
    )

    result = components.receiver.receive(_event("read the Drive fixture"))

    assert result.disposition == "failed"
    assert result.reason is not None
    assert len(components.outbound.sent) == 1
    assert "could not complete" in components.outbound.sent[0].body
    assert "action authority" not in components.outbound.sent[0].body


def test_agents_adapter_classifies_a_whole_tool_timeout_as_unavailable() -> None:
    def delayed_read(
        _request: OrchestrationRequest, _input: BoundedReadInput, _deadline: float
    ) -> BoundedReadOutput:
        time.sleep(0.05)
        return BoundedReadOutput(source="authorized_request", text="late")

    read_tool = BoundedReadTool(
        "read_request_context",
        "A deliberately delayed bounded read.",
        BoundedReadInput,
        BoundedReadOutput,
        delayed_read,
        timeout_seconds=0.01,
    )

    def run_sync(agent: object, _text: str, **_kwargs: object) -> object:
        asyncio.run(agent.tools[0].on_invoke_tool(None, "{}"))
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="The late read was ignored.",
            )
        )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        read_tool=read_tool,
    ).run(_request("read the current request context"))

    assert result.outcome == "unavailable"
    assert result.reply_text == (
        "The requested request context read could not be completed because the "
        "service timed out. I did not retry the unavailable read."
    )


def test_agents_adapter_returns_unavailable_when_service_identity_is_rejected() -> None:
    def rejected_read(
        _request: OrchestrationRequest, _input: BoundedReadInput, _deadline: float
    ) -> BoundedReadOutput:
        from jarvis_control_plane.service_protocol import ServiceAuthenticationError

        raise ServiceAuthenticationError("service response identity did not match")

    def run_sync(agent: object, _text: str, **_kwargs: object) -> object:
        asyncio.run(agent.tools[0].on_invoke_tool(None, "{}"))
        return SimpleNamespace(final_output=AgentsSdkPlan(reply_text="unreachable"))

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        read_tool=BoundedReadTool(
            "read_request_context",
            "A read with a rejected service identity.",
            BoundedReadInput,
            BoundedReadOutput,
            rejected_read,
        ),
    ).run(_request("read through the rejected service"))

    assert result.outcome == "unavailable"
    assert result.reply_text == (
        "The requested request context read could not be completed because the "
        "service identity could not be verified. I did not retry the unavailable read."
    )


def test_agents_adapter_cancels_the_async_model_turn_at_its_deadline() -> None:
    cancelled = Event()

    async def run_async(_agent: object, _text: str, **_kwargs: object) -> object:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_async=run_async,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        model_turn_timeout_seconds=0.01,
    )

    with pytest.raises(OrchestrationAdapterError, match="configured deadline"):
        adapter.run(_request("wait forever"))

    assert cancelled.is_set()


def test_agents_adapter_bounds_cancellation_quiescence_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarvis_control_plane import orchestration

    async def run_async(_agent: object, _text: str, **_kwargs: object) -> object:
        try:
            await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            await asyncio.sleep(0.02)
            raise

    monkeypatch.setattr(orchestration, "_MODEL_CANCELLATION_GRACE_SECONDS", 0.01)
    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_async=run_async,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        model_turn_timeout_seconds=0.01,
    )

    with pytest.raises(OrchestrationAdapterError, match="establish quiescence"):
        adapter.run(_request("delay cancellation"))


def test_agents_adapter_cancels_an_active_async_model_turn_and_waits_for_quiescence() -> (
    None
):
    started = Event()
    cancelled = Event()
    outcomes: list[OrchestrationAdapterError] = []

    async def run_async(_agent: object, _text: str, **_kwargs: object) -> object:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_async=run_async,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        model_turn_timeout_seconds=90,
    )

    def run() -> None:
        try:
            adapter.run(_request("wait for cancellation"))
        except OrchestrationAdapterError as exc:
            outcomes.append(exc)

    runner = Thread(target=run)
    runner.start()
    assert started.wait(timeout=2)

    assert adapter.cancel(request_id="request-001") is True
    assert cancelled.is_set()
    runner.join(timeout=2)

    assert not runner.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], OrchestrationAdapterError)
    assert "model turn was cancelled" in str(outcomes[0])


def test_agents_adapter_does_not_misreport_a_provider_timeout_as_its_deadline() -> None:
    async def run_async(_agent: object, _text: str, **_kwargs: object) -> object:
        raise TimeoutError("provider timed out first")

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_async=run_async,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        model_turn_timeout_seconds=1,
    )

    with pytest.raises(OrchestrationAdapterError, match="run was unavailable"):
        adapter.run(_request("provider timeout"))


def test_agents_adapter_enforces_a_per_request_read_invocation_limit() -> None:
    def run_sync(agent: object, _text: str, **_kwargs: object) -> object:
        read_tool = agent.tools[0]
        asyncio.run(read_tool.on_invoke_tool(None, "{}"))
        asyncio.run(read_tool.on_invoke_tool(None, "{}"))
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="unreachable",
            )
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        max_tool_invocations=1,
    )

    with pytest.raises(OrchestrationAdapterError, match="invocation limit"):
        adapter.run(_request("read the current request context twice"))
