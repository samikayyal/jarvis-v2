from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_support import build_receiver_components

from jarvis_control_plane.adapters import (
    DeterministicIdGenerator,
    FixedClock,
    InMemoryAuditBoundary,
)
from jarvis_control_plane.google_oauth import (
    InMemoryGoogleCredentialStore,
    OAuthCredentialRecord,
)
from jarvis_control_plane.google_reads import (
    GMAIL_READ_SCOPE,
    GOOGLE_READ_SCOPES,
    ControlledGoogleReadProvider,
    GoogleReadConnector,
    GoogleReadError,
    GoogleReadProviderResult,
)
from jarvis_control_plane.models import InboundMessage, SignedInboundEvent
from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
)
from jarvis_control_plane.ports import AuditWriteError

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
IDENTITY = "google-subject-123"


class _FakeReasoning:
    def __init__(self, *, effort: str) -> None:
        self.effort = effort


class _FakeModelSettings:
    def __init__(self, **values: object) -> None:
        self.values = values


class _FakeRunConfig:
    def __init__(self, **values: object) -> None:
        self.values = values


def _connector(
    *,
    provider: ControlledGoogleReadProvider | None = None,
    identity: str = IDENTITY,
    scopes: frozenset[str] = GOOGLE_READ_SCOPES,
    audit: InMemoryAuditBoundary | None = None,
    clock: FixedClock | None = None,
    ids: DeterministicIdGenerator | None = None,
) -> GoogleReadConnector:
    return GoogleReadConnector(
        configured_identity=IDENTITY,
        credential_store=InMemoryGoogleCredentialStore(
            OAuthCredentialRecord(
                subject=identity,
                granted_scopes=scopes,
                refresh_token="controlled-refresh-token",
            )
        ),
        provider=provider or ControlledGoogleReadProvider(),
        audit=audit or InMemoryAuditBoundary(),
        clock=clock or FixedClock(NOW),
        ids=ids or DeterministicIdGenerator("ticket17-google"),
    )


def _event(text: str, *, event_id: str = "event-ticket17") -> SignedInboundEvent:
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
        b"ticket17-test-secret",
    )


def test_gmail_read_is_fixed_to_approved_scope_and_returns_a_bounded_result() -> None:
    provider = ControlledGoogleReadProvider(
        result=GoogleReadProviderResult(items=("x" * (10 * 1024 + 100),) * 21)
    )
    connector = _connector(provider=provider)

    result = connector.gmail_messages_list(
        request_id="request-001", query="from:inbox", max_results=50
    )

    assert result.operation == "gmail_messages_list"
    assert len(result.items) == 20
    assert all(len(item.encode("utf-8")) <= 16 * 1024 for item in result.items)
    assert result.truncated is True
    assert result.continuation_available is True
    assert provider.calls == [("gmail_messages_list", {"query": "from:inbox"}, 20)]
    assert "refresh_token" not in repr(result)


@pytest.mark.parametrize(
    ("identity", "scopes", "expected"),
    (
        ("wrong-subject", GOOGLE_READ_SCOPES, "wrong_identity"),
        (IDENTITY, frozenset({GMAIL_READ_SCOPE}), "missing_scope"),
    ),
)
def test_google_read_rejects_wrong_identity_or_scope_before_the_provider(
    identity: str, scopes: frozenset[str], expected: str
) -> None:
    provider = ControlledGoogleReadProvider()

    with pytest.raises(GoogleReadError, match=expected):
        _connector(
            provider=provider, identity=identity, scopes=scopes
        ).drive_files_list(request_id="request-001", query="report")

    assert provider.calls == []


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        ("timeout", "google_read_timeout"),
        ("rate_limited", "google_read_rate_limited"),
        ("provider exploded: controlled-refresh-token", "google_read_unavailable"),
    ),
)
def test_google_read_failures_are_sanitized(failure: str, expected: str) -> None:
    with pytest.raises(GoogleReadError, match=expected) as caught:
        _connector(
            provider=ControlledGoogleReadProvider(failure=failure)
        ).calendar_list(request_id="request-001")

    assert "controlled-refresh-token" not in str(caught.value)


def test_google_read_refuses_an_oversized_serialized_result() -> None:
    connector = _connector(
        provider=ControlledGoogleReadProvider(
            result=GoogleReadProviderResult(items=("x" * (16 * 1024),) * 20)
        )
    )

    with pytest.raises(GoogleReadError, match="google_read_oversized"):
        connector.gmail_messages_list(request_id="request-001", query="from:inbox")


def test_google_read_audit_failure_prevents_the_provider_call() -> None:
    class _UnavailableAudit(InMemoryAuditBoundary):
        def append(self, _evidence: object) -> None:
            raise AuditWriteError("controlled audit outage")

    provider = ControlledGoogleReadProvider()
    connector = _connector(provider=provider, audit=_UnavailableAudit())

    with pytest.raises(GoogleReadError, match="google_read_audit_unavailable"):
        connector.drive_files_list(request_id="request-001", query="report")

    assert provider.calls == []


def test_signed_request_reaches_only_closed_google_read_tools_through_broker() -> None:
    captured: dict[str, object] = {}
    audit = InMemoryAuditBoundary()
    clock = FixedClock(NOW)
    ids = DeterministicIdGenerator("ticket17-broker")
    connector = _connector(
        provider=ControlledGoogleReadProvider(
            result=GoogleReadProviderResult(items=("Subject: bounded mail",))
        ),
        audit=audit,
        clock=clock,
        ids=ids,
    )

    def agent_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    def run_sync(agent: object, _text: str, **_kwargs: object) -> object:
        tools = agent.tools
        assert [tool.name for tool in tools] == [
            "read_request_context",
            "read_gmail",
            "read_google_calendar",
            "read_google_drive",
        ]
        gmail = tools[1]
        captured["tool_result"] = asyncio.run(
            gmail.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "operation": "messages_list",
                        "query": "from:inbox",
                        "max_results": 1,
                    }
                ),
            )
        )
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I found one bounded Gmail result.",
                execution_host="ubuntu",
                host_reason_code="default_ubuntu",
            )
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=agent_factory,
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        google_read_connector=connector,
    )
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.test",
        signing_secret=b"ticket17-test-secret",
        now=NOW,
        id_prefix="ticket17-broker",
        audit=audit,
        clock=clock,
        ids=ids,
        orchestration=adapter,  # type: ignore[arg-type]
    )

    result = components.receiver.receive(_event("read my inbox"))

    assert result.disposition == "completed"
    assert captured["tool_result"] == {
        "service": "gmail",
        "operation": "gmail_messages_list",
        "items": ["Subject: bounded mail"],
        "truncated": False,
        "continuation_available": False,
    }
    assert all(tool.needs_approval is False for tool in captured["tools"])
    assert len(components.outbound.sent) == 1
    assert any(
        record.kind == "google_read" and record.execution_status == "completed"
        for record in audit.records
    )
