from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from jarvis_personal_runtime.mcp import (
    ConfiguredMcpService,
    GoogleConnectionManager,
    GoogleOAuthTokenProvider,
    HttpMcpTransport,
    McpConnection,
    McpDiscovery,
    McpManifestError,
    McpServiceConfig,
    McpTransportError,
    load_operation_manifest,
)
from jarvis_personal_runtime.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    Completed,
    InboundText,
    PersonalRuntime,
)

DISCOVERED_TOOL = {
    "name": "create_event",
    "description": "Create an event.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "startTime": {"type": "string"},
            "endTime": {"type": "string"},
        },
        "required": ["summary", "startTime", "endTime"],
    },
    "annotations": {"destructiveHint": False, "idempotentHint": False},
}

ROOT = Path(__file__).resolve().parents[2]


def manifest_payload(*, mode: str = "write") -> dict[str, object]:
    prepared_schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 200},
            "startTime": {"type": "string", "maxLength": 64},
            "endTime": {"type": "string", "maxLength": 64},
        },
        "required": ["summary", "startTime", "endTime"],
        "additionalProperties": False,
    }
    return {
        "manifest_version": 1,
        "service": {
            "id": "google-calendar",
            "endpoint": "https://calendarmcp.googleapis.com/mcp/v1",
            "protocol_version": "2025-06-18",
            "server_info": {"name": "StatelessServer", "version": "ESF"},
        },
        "captured_at": "2026-09-02",
        "operations": [
            {
                "upstream": DISCOVERED_TOOL,
                "prepared": {
                    "name": "google_calendar_create_event",
                    "description": "Create one Google Calendar event.",
                    "input_schema": prepared_schema,
                },
                "mode": mode,
            }
        ],
    }


class FakeTransport:
    def __init__(self) -> None:
        self.discovery = McpDiscovery(
            protocol_version="2025-06-18",
            server_info={"name": "StatelessServer", "version": "ESF"},
            tools=(DISCOVERED_TOOL, {"name": "unselected_tool"}),
        )
        self.calls: list[tuple[str, dict[str, object], str]] = []
        self.error: Exception | None = None

    async def discover(self, endpoint: str, protocol_version: str) -> McpDiscovery:
        return self.discovery

    async def call(
        self,
        endpoint: str,
        protocol_version: str,
        operation: str,
        arguments: dict[str, object],
        connection: McpConnection,
    ) -> object:
        self.calls.append((operation, arguments, connection.id))
        if self.error:
            raise self.error
        return {"event": "created"}


def build_service(
    payload: dict[str, object], transport: FakeTransport
) -> ConfiguredMcpService:
    manifest = load_operation_manifest(payload)
    config = McpServiceConfig(
        id="google-calendar",
        endpoint="https://calendarmcp.googleapis.com/mcp/v1",
        manifest_path=Path("manifests/google-calendar.json"),
        max_output_chars=2_000,
    )
    return asyncio.run(ConfiguredMcpService.prepare(config, manifest, transport))


def event_args() -> dict[str, object]:
    return {
        "summary": "Dentist",
        "startTime": "2026-09-03T09:00:00+03:00",
        "endTime": "2026-09-03T10:00:00+03:00",
    }


def test_prepare_exposes_only_manifest_selected_operations_after_exact_discovery() -> (
    None
):
    service = build_service(manifest_payload(), FakeTransport())

    assert [definition["name"] for definition in service.definitions] == [
        "google_calendar_create_event"
    ]
    assert service.definitions[0]["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize("drift", ["protocol", "server", "schema", "annotations"])
def test_prepare_fails_closed_on_selected_contract_drift(drift: str) -> None:
    transport = FakeTransport()
    if drift == "protocol":
        transport.discovery = McpDiscovery(
            "2099-01-01", transport.discovery.server_info, transport.discovery.tools
        )
    elif drift == "server":
        transport.discovery.server_info["version"] = "changed"
    else:
        changed = dict(DISCOVERED_TOOL)
        changed["inputSchema" if drift == "schema" else "annotations"] = {}
        transport.discovery = McpDiscovery(
            transport.discovery.protocol_version,
            transport.discovery.server_info,
            (changed,),
        )

    with pytest.raises(McpManifestError, match="discovery does not match manifest"):
        build_service(manifest_payload(), transport)


def test_write_freezes_exact_arguments_and_expires_with_google_connection() -> None:
    transport = FakeTransport()
    service = build_service(manifest_payload(), transport)
    service.bind(McpConnection("google-link-1", "person@example.com"))

    arguments = event_args()
    proposed = asyncio.run(service.execute("google_calendar_create_event", arguments))

    assert isinstance(proposed, ApprovalRequired)
    assert proposed.action.allow_save_permission is False
    assert proposed.action.display == (
        "Run Google write?\n"
        "Connection: person@example.com\n"
        "Service: google-calendar\n"
        "Operation: create_event\n"
        'Arguments: {"endTime":"2026-09-03T10:00:00+03:00",'
        '"startTime":"2026-09-03T09:00:00+03:00","summary":"Dentist"}'
    )

    arguments["summary"] = "Changed elsewhere"
    service.bind(McpConnection("google-link-2", "person@example.com"))
    expired = asyncio.run(service.resume(proposed.continuation, approved=True))

    assert json.loads(expired) == {"error": {"kind": "connection_changed"}}
    assert transport.calls == []


def test_rejected_write_is_resolved_without_a_remote_call() -> None:
    transport = FakeTransport()
    service = build_service(manifest_payload(), transport)
    service.bind(McpConnection("google-link-1", "person@example.com"))
    proposed = asyncio.run(
        service.execute("google_calendar_create_event", event_args())
    )
    assert isinstance(proposed, ApprovalRequired)

    rejected = asyncio.run(service.resume(proposed.continuation, approved=False))

    assert json.loads(rejected) == {"rejected": True}
    assert transport.calls == []


def test_read_calls_once_and_normalizes_failure_without_automatic_retry() -> None:
    transport = FakeTransport()
    transport.error = McpTransportError("network went away")
    service = build_service(manifest_payload(mode="read"), transport)
    service.bind(McpConnection("google-link-1", "person@example.com"))

    result = asyncio.run(service.execute("google_calendar_create_event", event_args()))

    assert json.loads(result) == {"error": {"kind": "unavailable"}}
    assert len(transport.calls) == 1


def test_approved_write_is_attempted_once_and_ambiguous_failure_is_not_retried() -> (
    None
):
    transport = FakeTransport()
    service = build_service(manifest_payload(), transport)
    service.bind(McpConnection("google-link-1", "person@example.com"))
    proposed = asyncio.run(
        service.execute("google_calendar_create_event", event_args())
    )
    assert isinstance(proposed, ApprovalRequired)
    transport.error = McpTransportError("connection reset")

    first = asyncio.run(service.resume(proposed.continuation, approved=True))
    second = asyncio.run(service.resume(proposed.continuation, approved=True))

    assert json.loads(first) == {"error": {"kind": "outcome_ambiguous"}}
    assert json.loads(second) == {"error": {"kind": "already_resolved"}}
    assert len(transport.calls) == 1


def test_manifest_and_config_identity_must_match() -> None:
    transport = FakeTransport()
    manifest = load_operation_manifest(manifest_payload())
    wrong = McpServiceConfig(
        id="other",
        endpoint="https://calendarmcp.googleapis.com/mcp/v1",
        manifest_path=Path("manifest.json"),
        max_output_chars=2_000,
    )

    with pytest.raises(McpManifestError, match="configured service identity"):
        asyncio.run(ConfiguredMcpService.prepare(wrong, manifest, transport))


def test_direct_construction_cannot_bypass_discovery() -> None:
    manifest = load_operation_manifest(manifest_payload())
    config = McpServiceConfig(
        id="google-calendar",
        endpoint="https://calendarmcp.googleapis.com/mcp/v1",
        manifest_path=Path("manifest.json"),
        max_output_chars=2_000,
    )

    with pytest.raises(TypeError, match="use ConfiguredMcpService.prepare"):
        ConfiguredMcpService(config, manifest, FakeTransport())


def test_manifest_rejects_open_prepared_schemas() -> None:
    payload = manifest_payload()
    prepared = payload["operations"][0]["prepared"]  # type: ignore[index]
    prepared["input_schema"]["additionalProperties"] = True  # type: ignore[index]

    with pytest.raises(McpManifestError, match="closed object schema"):
        load_operation_manifest(payload)


def test_nested_prepared_schema_is_enforced() -> None:
    payload = manifest_payload()
    schema = payload["operations"][0]["prepared"]["input_schema"]  # type: ignore[index]
    schema["properties"]["attendees"] = {  # type: ignore[index]
        "type": "array",
        "maxItems": 2,
        "items": {"type": "string", "maxLength": 100},
    }
    service = build_service(payload, FakeTransport())
    service.bind(McpConnection("google-link-1", "person@example.com"))
    arguments = {**event_args(), "attendees": ["a@example.com", 7]}

    with pytest.raises(ValueError, match=r"arguments\.attendees\[1\] must be string"):
        asyncio.run(service.execute("google_calendar_create_event", arguments))


def test_invalid_result_and_unexpected_transport_failure_are_normalized() -> None:
    transport = FakeTransport()
    service = build_service(manifest_payload(mode="read"), transport)
    service.bind(McpConnection("google-link-1", "person@example.com"))

    transport.error = RuntimeError("socket failed")
    unexpected = asyncio.run(
        service.execute("google_calendar_create_event", event_args())
    )
    transport.error = None

    async def invalid_call(*args: object, **kwargs: object) -> object:
        return object()

    transport.call = invalid_call  # type: ignore[method-assign]
    invalid = asyncio.run(service.execute("google_calendar_create_event", event_args()))

    assert json.loads(unexpected) == {"error": {"kind": "unavailable"}}
    assert json.loads(invalid) == {"error": {"kind": "invalid_response"}}


def test_transport_error_kind_is_bounded() -> None:
    with pytest.raises(ValueError, match="unsupported MCP transport error kind"):
        McpTransportError("failed", kind="x" * 10_000)


def test_server_identity_uses_canonical_json_comparison() -> None:
    transport = FakeTransport()
    transport.discovery.server_info["build"] = 1.0
    payload = manifest_payload()
    payload["service"]["server_info"]["build"] = 1  # type: ignore[index]

    with pytest.raises(McpManifestError, match="discovery does not match manifest"):
        build_service(payload, transport)


def test_manifest_rejects_malformed_enums() -> None:
    payload = manifest_payload()
    schema = payload["operations"][0]["prepared"]["input_schema"]  # type: ignore[index]
    schema["properties"]["summary"]["enum"] = "Dentist"  # type: ignore[index]

    with pytest.raises(McpManifestError, match="enum must be a non-empty typed array"):
        load_operation_manifest(payload)


def test_service_composes_with_runtime_and_choice_two_stays_terminal_only() -> None:
    transport = FakeTransport()
    service = build_service(manifest_payload(), transport)
    service.bind(McpConnection("google-link-1", "person@example.com"))

    class Runner:
        async def run(self, *args: object, **kwargs: object) -> object:
            return await service.execute("google_calendar_create_event", event_args())

        async def resume(
            self, decision: ApprovalDecision, continuation: object
        ) -> Completed:
            result = await service.resume(
                continuation, approved=decision is ApprovalDecision.APPROVE_ONCE
            )
            return Completed(result)

        def cancel_pending(self, continuation: object) -> None:
            return None

    async def scenario() -> None:
        runtime = PersonalRuntime(request_runner=Runner())  # type: ignore[arg-type]
        now = datetime.fromisoformat("2026-09-02T00:00:00+00:00")
        pending = await runtime.receive(InboundText("m1", "create it", now))
        ignored = await runtime.receive(InboundText("m2", "2", now))
        approved = await runtime.receive(InboundText("m3", "1", now))

        assert pending.disposition == "approval_required"
        assert ignored.disposition == "ignored"
        assert json.loads(approved.replies[0]) == {"result": {"event": "created"}}

    asyncio.run(scenario())
    assert len(transport.calls) == 1


def test_http_transport_uses_streamable_http_lifecycle_and_never_traces_bearer() -> (
    None
):
    requests: list[httpx.Request] = []
    responses = iter(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "StatelessServer", "version": "ESF"},
                },
            },
            None,
            {"jsonrpc": "2.0", "id": 2, "result": {"tools": [DISCOVERED_TOOL]}},
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "StatelessServer", "version": "ESF"},
                },
            },
            None,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = next(responses)
        return httpx.Response(202 if payload is None else 200, json=payload)

    class Tokens:
        async def access_token(self) -> str:
            return "secret-access-token"

    class Trace:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        def record(self, event: str, payload: dict[str, object]) -> None:
            self.events.append((event, payload))

    trace = Trace()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpMcpTransport(
        Tokens(),  # type: ignore[arg-type]
        authorized_endpoints={"https://calendar.example/mcp"},
        client=client,
        trace=trace,
    )

    async def scenario() -> None:
        discovery = await transport.discover(
            "https://calendar.example/mcp", "2025-06-18"
        )
        assert discovery.tools == (DISCOVERED_TOOL,)
        result = await transport.call(
            "https://calendar.example/mcp",
            "2025-06-18",
            "create_event",
            event_args(),
            McpConnection("link", "Google account"),
        )
        assert result == {"content": [{"type": "text", "text": "ok"}]}
        await client.aclose()

    asyncio.run(scenario())

    assert len(requests) == 6
    assert "secret-access-token" not in json.dumps(trace.events)
    assert requests[-1].headers["authorization"] == "Bearer secret-access-token"
    assert requests[-1].headers["mcp-protocol-version"] == "2025-06-18"


def test_http_transport_never_sends_google_token_to_an_unconfigured_endpoint() -> None:
    class Tokens:
        def __init__(self) -> None:
            self.calls = 0

        async def access_token(self) -> str:
            self.calls += 1
            return "secret"

    tokens = Tokens()
    transport = HttpMcpTransport(
        tokens,  # type: ignore[arg-type]
        authorized_endpoints={"https://calendar.example/mcp"},
    )

    with pytest.raises(McpTransportError, match="not authorized"):
        asyncio.run(
            transport.call(
                "https://attacker.example/mcp",
                "2025-06-18",
                "create_event",
                event_args(),
                McpConnection("link", "Google account"),
            )
        )

    assert tokens.calls == 0


def test_google_oauth_token_is_refreshed_after_expiry() -> None:
    issued = iter(("token-one", "token-two"))
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert b"refresh-token" in request.content
        return httpx.Response(200, json={"access_token": next(issued), "expires_in": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tokens = GoogleOAuthTokenProvider(
        "client-id", "client-secret", "refresh-token", client=client
    )

    async def scenario() -> None:
        assert await tokens.access_token() == "token-one"
        assert await tokens.access_token() == "token-two"
        await client.aclose()

    asyncio.run(scenario())
    assert calls == 2


def test_google_connection_refreshes_once_and_disconnect_invalidates_services() -> None:
    transport = FakeTransport()
    service = build_service(manifest_payload(), transport)

    class Tokens:
        def __init__(self) -> None:
            self.refreshes = 0

        async def refresh(self) -> None:
            self.refreshes += 1

    tokens = Tokens()
    connections = GoogleConnectionManager((service,), tokens)  # type: ignore[arg-type]

    async def scenario() -> None:
        assert connections.status() == "Google: disconnected"
        assert await connections.connect() == "Connected Google account."
        proposed = await service.execute("google_calendar_create_event", event_args())
        assert isinstance(proposed, ApprovalRequired)
        assert connections.disconnect() == "Disconnected Google account."
        expired = await service.resume(proposed.continuation, approved=True)
        assert json.loads(expired) == {"error": {"kind": "connection_changed"}}

    asyncio.run(scenario())
    assert tokens.refreshes == 1
    assert transport.calls == []


def test_checked_in_google_manifests_expose_only_bounded_selected_operations() -> None:
    manifests = ROOT / "deployment" / "personal-runtime" / "manifests"
    prepared = {
        path.stem: load_operation_manifest(path) for path in manifests.glob("*.json")
    }

    assert set(prepared) == {"google-gmail", "google-drive", "google-calendar"}
    assert {
        operation.prepared_name for operation in prepared["google-gmail"].operations
    } == {
        "google_gmail_search",
        "google_gmail_read_thread",
        "google_gmail_read_message",
    }
    assert all(
        operation.mode == "read" for operation in prepared["google-gmail"].operations
    )
    assert {
        operation.prepared_name
        for operation in prepared["google-calendar"].operations
        if operation.mode == "write"
    } == {"google_calendar_create", "google_calendar_update"}


def test_calendar_list_rejects_an_unbounded_or_reversed_time_window() -> None:
    payload = manifest_payload(mode="read")
    operation = payload["operations"][0]  # type: ignore[index]
    operation["prepared"]["name"] = "google_calendar_list"  # type: ignore[index]
    operation["prepared"]["input_schema"] = {  # type: ignore[index]
        "type": "object",
        "properties": {
            "calendarId": {"type": "string", "enum": ["primary"], "maxLength": 7},
            "startTime": {"type": "string", "maxLength": 64},
            "endTime": {"type": "string", "maxLength": 64},
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["calendarId", "startTime", "endTime", "pageSize"],
        "additionalProperties": False,
    }
    transport = FakeTransport()
    service = build_service(payload, transport)
    service.bind(McpConnection("link", "Google account"))

    for end_time in (
        "2026-08-01T00:00:00Z",
        "2027-01-01T00:00:00Z",
        "not-a-timestamp",
    ):
        with pytest.raises(ValueError, match="time window"):
            asyncio.run(
                service.execute(
                    "google_calendar_list",
                    {
                        "calendarId": "primary",
                        "startTime": "2026-09-01T00:00:00Z",
                        "endTime": end_time,
                        "pageSize": 20,
                    },
                )
            )
