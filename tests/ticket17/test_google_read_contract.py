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


def test_v1_google_read_tools_exclude_calendar() -> None:
    assert {tool.name for tool in _google_read_tools(_connector())} == {
        "read_gmail",
        "read_google_drive",
    }


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


def test_google_readiness_is_a_safe_connected_service_projection() -> None:
    connector = _connector()
    assert connector.current() == ServiceReadiness("google", "ready")

    disconnected = _connector(
        credential_store=InMemoryGoogleCredentialStore(),
    )
    assert disconnected.current() == ServiceReadiness("google", "unavailable")


def test_broker_status_persists_the_google_readiness_projection() -> None:
    class FixedGoogleReadiness:
        @staticmethod
        def current() -> ServiceReadiness:
            return ServiceReadiness("google", "ready")

    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.test",
        signing_secret=b"ticket17-test-secret",
        now=NOW,
        id_prefix="ticket17-status-google",
        google_readiness_provider=FixedGoogleReadiness(),
    )

    result = components.receiver.receive(_event("/status"))

    assert result.reply is not None
    assert "connected services=google=ready" in result.reply.body
    assert "credential" not in result.reply.body.lower()
    assert "scope" not in result.reply.body.lower()


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
        ).drive_files_list(request_id="request-001", query="name = 'fixture'")

    assert "controlled-refresh-token" not in str(caught.value)


def test_invalid_grant_discards_the_credential_before_reporting_disconnection() -> None:
    store = InMemoryGoogleCredentialStore(
        OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=GOOGLE_READ_SCOPES,
            refresh_token="controlled-refresh-token",
            connection_generation=1,
        )
    )
    invalidations: list[str] = []

    def invalidate(connection_generation: int) -> None:
        assert connection_generation == 1
        store.delete()
        invalidations.append("invalidated")

    with pytest.raises(GoogleReadError, match="google_read_disconnected"):
        _connector(
            credential_store=store,
            provider=ControlledGoogleReadProvider(failure="invalid_grant"),
            on_invalid_grant=invalidate,
        ).drive_files_list(request_id="request-001", query="name = 'fixture'")

    assert store.current is None
    assert invalidations == ["invalidated"]


def test_disconnected_read_records_attempt_and_failed_audit_evidence() -> None:
    audit = InMemoryAuditBoundary()
    connector = _connector(
        audit=audit,
        credential_store=InMemoryGoogleCredentialStore(),
    )

    with pytest.raises(GoogleReadError, match="google_read_disconnected"):
        connector.drive_files_list(
            request_id="request-disconnected",
            query="name = 'fixture'",
            max_results=1,
        )

    evidence = audit.safe_view()
    assert [(item.outcome, item.execution_status) for item in evidence] == [
        ("attempted", "attempted"),
        ("failed", "failed"),
    ]
    assert all(item.request_id == "request-disconnected" for item in evidence)


def test_local_disconnected_read_returns_sanitized_orchestration_result() -> None:
    connector = _connector(credential_store=InMemoryGoogleCredentialStore())

    def run_sync(agent: object, _text: str, **_kwargs: object) -> object:
        drive = next(tool for tool in agent.tools if tool.name == "read_google_drive")
        asyncio.run(
            drive.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "operation": "files_list",
                        "query": "name = 'fixture'",
                        "max_results": 1,
                    }
                ),
            )
        )
        return SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="Ignore the unavailable result.")
        )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        google_read_connector=connector,
    ).run(
        OrchestrationRequest(
            state=RequestState(
                request_id="request-local-disconnected",
                event_id="event-local-disconnected",
                message_id="message-local-disconnected",
                operator_id="operator.test",
                session_id="session.test",
                chat_id="operator.test",
                created_at=NOW,
                updated_at=NOW,
                status="running",
                phase="orchestration",
                model="gpt-5.6-terra",
                reasoning="medium",
            ),
            text="List one Drive fixture without modifying it.",
        )
    )

    assert result.outcome == "unavailable"
    assert result.reply_text == (
        "The requested Google Drive read could not be completed because Google is "
        "disconnected. I did not retry the unavailable read."
    )
    assert result.proposal is None


def test_disconnected_read_propagates_failure_audit_outage() -> None:
    audit = InMemoryAuditBoundary(fail_on_append=2)
    connector = _connector(
        audit=audit,
        credential_store=InMemoryGoogleCredentialStore(),
    )

    with pytest.raises(GoogleReadError, match="google_read_audit_unavailable"):
        connector.drive_files_list(
            request_id="request-disconnected-audit-outage",
            query="name = 'fixture'",
            max_results=1,
        )

    evidence = audit.safe_view()
    assert [(item.outcome, item.execution_status) for item in evidence] == [
        ("attempted", "attempted"),
    ]
