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


@pytest.mark.parametrize(
    ("read_request", "response_payload", "expected_path", "page_size_key"),
    (
        (
            GoogleReadRequest("gmail_messages_list", {"query": "from:inbox"}, 2),
            {"messages": [{"id": "m1"}], "nextPageToken": "next"},
            "/gmail/v1/users/me/messages",
            "maxResults",
        ),
        (
            GoogleReadRequest("gmail_messages_get", {"message_id": "m1"}, 1),
            {"id": "m1", "snippet": "bounded"},
            "/gmail/v1/users/me/messages/m1",
            None,
        ),
        (
            GoogleReadRequest("gmail_threads_list", {"query": "label:inbox"}, 2),
            {"threads": [{"id": "t1"}]},
            "/gmail/v1/users/me/threads",
            "maxResults",
        ),
        (
            GoogleReadRequest("gmail_threads_get", {"thread_id": "t1"}, 1),
            {"id": "t1", "messages": []},
            "/gmail/v1/users/me/threads/t1",
            None,
        ),
        (
            GoogleReadRequest("drive_files_list", {"query": "name contains 'plan'"}, 2),
            {"files": [{"id": "file1"}]},
            "/drive/v3/files",
            "pageSize",
        ),
        (
            GoogleReadRequest("drive_files_get", {"file_id": "file1"}, 1),
            {"id": "file1", "name": "plan"},
            "/drive/v3/files/file1",
            None,
        ),
    ),
)
def test_live_provider_uses_only_the_fixed_google_read_operations(
    read_request: GoogleReadRequest,
    response_payload: object,
    expected_path: str,
    page_size_key: str | None,
) -> None:
    transport = _RecordingGoogleTransport(
        [
            _json_response({"access_token": "access-token"}),
            _json_response(response_payload),
        ]
    )
    provider = GoogleApiReadProvider(
        client_id="client-id", client_secret="client-secret", transport=transport
    )

    result = provider.read(
        request=read_request,
        credential=OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=GOOGLE_READ_SCOPES,
            refresh_token="refresh-token",
        ),
    )

    assert len(transport.calls) == 2
    call = transport.calls[1]
    parsed = urlparse(call["url"])  # type: ignore[arg-type]
    query = parse_qs(parsed.query)
    assert call["method"] == "GET"
    assert parsed.path == expected_path
    assert "pageToken" not in query
    assert "fields" in query
    if page_size_key is not None:
        assert query[page_size_key] == [str(read_request.max_results)]
    assert result.items


def test_live_provider_exports_only_text_and_classifies_invalid_grant() -> None:
    export_transport = _RecordingGoogleTransport(
        [
            _json_response({"access_token": "access-token"}),
            GoogleReadHttpResponse(
                status_code=200,
                headers={"Content-Type": "text/plain; charset=utf-8"},
                body=b"plain document",
            ),
        ]
    )
    provider = GoogleApiReadProvider(
        client_id="client-id", client_secret="client-secret", transport=export_transport
    )
    credential = OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=GOOGLE_READ_SCOPES,
        refresh_token="refresh-token",
    )
    assert provider.read(
        request=GoogleReadRequest(
            "drive_files_export", {"file_id": "doc1", "mime_type": "text/plain"}, 1
        ),
        credential=credential,
    ).items == ("plain document",)

    invalid_grant_transport = _RecordingGoogleTransport(
        [_json_response({"error": "invalid_grant"}, status_code=400)]
    )
    with pytest.raises(GoogleReadProviderError, match="invalid_grant"):
        GoogleApiReadProvider(
            client_id="client-id",
            client_secret="client-secret",
            transport=invalid_grant_transport,
        ).read(
            request=GoogleReadRequest(
                "drive_files_list", {"query": "name = 'fixture'"}, 1
            ),
            credential=credential,
        )


def test_live_provider_accepts_case_insensitive_text_export_content_type() -> None:
    transport = _RecordingGoogleTransport(
        [
            _json_response({"access_token": "access-token"}),
            GoogleReadHttpResponse(
                status_code=200,
                headers={"content-type": "text/plain; charset=utf-8"},
                body=b"plain document",
            ),
        ]
    )
    provider = GoogleApiReadProvider(
        client_id="client-id", client_secret="client-secret", transport=transport
    )

    result = provider.read(
        request=GoogleReadRequest(
            "drive_files_export", {"file_id": "doc1", "mime_type": "text/plain"}, 1
        ),
        credential=OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=GOOGLE_READ_SCOPES,
            refresh_token="refresh-token",
        ),
    )

    assert result.items == ("plain document",)


def test_live_provider_reads_only_inline_textual_gmail_parts() -> None:
    plain_text = base64.urlsafe_b64encode(b"Please review the proposal.").decode()
    html_text = base64.urlsafe_b64encode(
        b"<p>HTML fallback</p><script>do-not-include</script>"
    ).decode()
    attachment = base64.urlsafe_b64encode(b"do-not-download-this").decode()
    transport = _RecordingGoogleTransport(
        [
            _json_response({"access_token": "access-token"}),
            _json_response(
                {
                    "id": "m1",
                    "threadId": "t1",
                    "snippet": "Please review",
                    "payload": {
                        "mimeType": "multipart/mixed",
                        "headers": [
                            {"name": "Subject", "value": "Proposal"},
                            {"name": "Message-ID", "value": "<m1@example.test>"},
                            {"name": "References", "value": "<root@example.test>"},
                            {"name": "Bcc", "value": "never-returned@example.test"},
                        ],
                        "parts": [
                            {"mimeType": "text/plain", "body": {"data": plain_text}},
                            {"mimeType": "text/html", "body": {"data": html_text}},
                            {
                                "mimeType": "text/plain",
                                "filename": "secret.txt",
                                "body": {"data": attachment},
                            },
                            {
                                "mimeType": "text/plain",
                                "body": {"attachmentId": "attachment-1"},
                            },
                        ],
                    },
                }
            ),
        ]
    )
    provider = GoogleApiReadProvider(
        client_id="client-id", client_secret="client-secret", transport=transport
    )
    result = provider.read(
        request=GoogleReadRequest("gmail_messages_get", {"message_id": "m1"}, 1),
        credential=OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=GOOGLE_READ_SCOPES,
            refresh_token="refresh-token",
        ),
    )

    request_query = parse_qs(urlparse(transport.calls[1]["url"]).query)  # type: ignore[arg-type]
    item = json.loads(result.items[0])
    assert request_query["format"] == ["full"]
    assert request_query["metadataHeaders"] == [
        "From",
        "To",
        "Cc",
        "Subject",
        "Date",
        "Message-ID",
        "References",
    ]
    assert "body(size,data,attachmentId)" in request_query["fields"][0]
    assert item == {
        "body": "Please review the proposal.\n\nHTML fallback",
        "headers": {
            "Message-ID": "<m1@example.test>",
            "References": "<root@example.test>",
            "Subject": "Proposal",
        },
        "id": "m1",
        "snippet": "Please review",
        "threadId": "t1",
    }
    assert "do-not-download-this" not in result.items[0]
    assert "attachment-1" not in result.items[0]


def test_live_provider_reads_approved_drive_media_after_metadata_only() -> None:
    transport = _RecordingGoogleTransport(
        [
            _json_response({"access_token": "access-token"}),
            _json_response(
                {"id": "file1", "name": "notes", "mimeType": "text/markdown"}
            ),
            GoogleReadHttpResponse(
                status_code=200,
                headers={"Content-Type": "text/markdown; charset=utf-8"},
                body=b"# Notes\nOnly text is read.",
            ),
        ]
    )
    provider = GoogleApiReadProvider(
        client_id="client-id", client_secret="client-secret", transport=transport
    )
    credential = OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=GOOGLE_READ_SCOPES,
        refresh_token="refresh-token",
    )

    result = provider.read(
        request=GoogleReadRequest("drive_files_get", {"file_id": "file1"}, 1),
        credential=credential,
    )

    assert len(transport.calls) == 3
    assert parse_qs(urlparse(transport.calls[1]["url"]).query)["fields"]  # type: ignore[arg-type]
    assert parse_qs(urlparse(transport.calls[2]["url"]).query) == {"alt": ["media"]}  # type: ignore[arg-type]
    assert json.loads(result.items[0]) == {
        "content": "# Notes\nOnly text is read.",
        "id": "file1",
        "mimeType": "text/markdown",
        "name": "notes",
    }


def test_live_provider_never_downloads_non_text_drive_media() -> None:
    transport = _RecordingGoogleTransport(
        [
            _json_response({"access_token": "access-token"}),
            _json_response({"id": "file1", "name": "photo", "mimeType": "image/png"}),
        ]
    )
    provider = GoogleApiReadProvider(
        client_id="client-id", client_secret="client-secret", transport=transport
    )

    result = provider.read(
        request=GoogleReadRequest("drive_files_get", {"file_id": "file1"}, 1),
        credential=OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=GOOGLE_READ_SCOPES,
            refresh_token="refresh-token",
        ),
    )

    assert len(transport.calls) == 2
    assert json.loads(result.items[0]) == {
        "content_unavailable": "unsupported_mime_type",
        "id": "file1",
        "mimeType": "image/png",
        "name": "photo",
    }
