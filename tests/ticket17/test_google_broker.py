# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from test_support import build_receiver_components

from jarvis_control_plane.adapters import (
    DeterministicIdGenerator,
    FixedClock,
    InMemoryAuditBoundary,
)
from jarvis_control_plane.google_oauth import (
    GoogleConnectionBinding,
    InMemoryGoogleCredentialStore,
    InMemoryGoogleOAuthStateStore,
    OAuthCredentialRecord,
)
from jarvis_control_plane.google_reads import (
    GMAIL_READ_SCOPE,
    GOOGLE_READ_SCOPES,
    ControlledGoogleReadProvider,
    GoogleApiReadProvider,
    GoogleReadConnector,
    GoogleReadError,
    GoogleReadHttpResponse,
    GoogleReadProviderError,
    GoogleReadProviderResult,
    GoogleReadRequest,
    _google_read_tools,
)
from jarvis_control_plane.manual_admin import _open_manual_trace_boundary
from jarvis_control_plane.models import (
    InboundMessage,
    OrchestrationRequest,
    RequestState,
    SignedInboundEvent,
)
from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
)
from jarvis_control_plane.ports import AuditWriteError
from jarvis_control_plane.sessions import ServiceReadiness
from jarvis_control_plane.traces import (
    DiagnosticTraceRecorder,
    InMemoryDiagnosticTraceStore,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
IDENTITY = "google-subject-123"


def _trace() -> DiagnosticTraceRecorder:
    store = InMemoryDiagnosticTraceStore()
    return DiagnosticTraceRecorder(
        writer=store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("trace"),
    )


class _FakeReasoning:
    def __init__(self, *, effort: str) -> None:
        self.effort = effort


class _FakeModelSettings:
    def __init__(self, **values: object) -> None:
        self.values = values


class _FakeRunConfig:
    def __init__(self, **values: object) -> None:
        self.values = values


class _RecordingGoogleTransport:
    def __init__(self, responses: list[GoogleReadHttpResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> GoogleReadHttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.responses.pop(0)


def _json_response(
    payload: object, *, status_code: int = 200
) -> GoogleReadHttpResponse:
    return GoogleReadHttpResponse(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )


def _connector(
    *,
    provider: object | None = None,
    identity: str = IDENTITY,
    scopes: frozenset[str] = GOOGLE_READ_SCOPES,
    audit: InMemoryAuditBoundary | None = None,
    clock: FixedClock | None = None,
    ids: DeterministicIdGenerator | None = None,
    trace: DiagnosticTraceRecorder | None = None,
    credential_store: InMemoryGoogleCredentialStore | None = None,
    on_invalid_grant: object | None = None,
) -> GoogleReadConnector:
    state_store = InMemoryGoogleOAuthStateStore()
    state_store.set_connection(connected=True, granted_scopes=scopes)
    connection = state_store.get_connection()
    if credential_store is None:
        credential_store = InMemoryGoogleCredentialStore(
            OAuthCredentialRecord(
                subject=identity,
                granted_scopes=scopes,
                refresh_token="controlled-refresh-token",
                connection_generation=connection.generation,
            )
        )
    return GoogleReadConnector(
        configured_identity=IDENTITY,
        credential_store=credential_store,
        provider=provider or ControlledGoogleReadProvider(),
        audit=audit or InMemoryAuditBoundary(),
        trace=trace or _trace(),
        clock=clock or FixedClock(NOW),
        ids=ids or DeterministicIdGenerator("ticket17-google"),
        connection_binding=GoogleConnectionBinding(
            state_store=state_store,
            credential_store=credential_store,
        ),
        on_invalid_grant=on_invalid_grant,  # type: ignore[arg-type]
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


def test_signed_unfiltered_gmail_list_reaches_closed_read_tool_through_broker() -> None:
    captured: dict[str, object] = {}
    audit = InMemoryAuditBoundary()
    clock = FixedClock(NOW)
    ids = DeterministicIdGenerator("ticket17-broker")
    trace_store = InMemoryDiagnosticTraceStore()
    trace = DiagnosticTraceRecorder(writer=trace_store.writer(), clock=clock, ids=ids)
    provider = ControlledGoogleReadProvider(
        result=GoogleReadProviderResult(items=("Subject: bounded mail",))
    )
    connector = _connector(
        provider=provider,
        audit=audit,
        trace=trace,
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
            "read_google_drive",
        ]
        gmail = tools[1]
        captured["tool_result"] = asyncio.run(
            gmail.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "operation": "messages_list",
                        "max_results": 1,
                    }
                ),
            )
        )
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I found one bounded Gmail result.",
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
        trace=trace,
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
    assert provider.calls == [("gmail_messages_list", {"query": ""}, 1)]
    assert all(tool.needs_approval is False for tool in captured["tools"])
    assert all(tool.timeout_seconds == 20.0 for tool in captured["tools"])
    assert len(components.outbound.sent) == 1
    assert any(
        record.kind == "google_read" and record.execution_status == "completed"
        for record in audit.records
    )
    traces = _open_manual_trace_boundary(trace_store).list_traces(
        operation_type="google_read_connector"
    )
    assert len(traces) == 1
    assert traces[0].arguments is not None
    assert traces[0].result is not None


def test_binary_drive_read_becomes_a_bounded_refusal_before_model_reply() -> None:
    audit = InMemoryAuditBoundary()
    connector = _connector(
        provider=ControlledGoogleReadProvider(
            result=GoogleReadProviderResult(
                items=(
                    json.dumps(
                        {
                            "content_unavailable": "unsupported_mime_type",
                            "id": "file1",
                            "mimeType": "image/png",
                        }
                    ),
                )
            ),
        ),
        audit=audit,
    )
    observed: dict[str, object] = {}

    def run_sync(agent: object, _text: str, **_kwargs: object) -> object:
        drive = next(tool for tool in agent.tools if tool.name == "read_google_drive")
        observed["tool_result"] = asyncio.run(
            drive.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "operation": "files_get",
                        "file_id": "file1",
                    }
                ),
            )
        )
        return SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="The binary file contains a chart.")
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
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
        id_prefix="ticket17-binary",
        audit=audit,
        orchestration=adapter,  # type: ignore[arg-type]
    )

    result = components.receiver.receive(_event("read the chart"))

    assert result.disposition == "unavailable"
    assert result.reply is not None
    assert "does not support reading binary file content" in result.reply.body
    assert "chart" not in result.reply.body
    assert observed["tool_result"] == {
        "unavailable": True,
        "message": (
            "The connected service is unavailable or not authorized. Explain that "
            "the requested read could not be completed, do not claim any retrieved "
            "data, and do not retry."
        ),
    }
