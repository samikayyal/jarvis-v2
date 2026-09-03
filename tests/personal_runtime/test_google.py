from __future__ import annotations

import asyncio
import base64
import json
from email import message_from_bytes
from email.policy import SMTP
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jarvis_personal_runtime.google import GoogleApiTools
from jarvis_personal_runtime.runtime import ApprovalRequired


class Tokens:
    def __init__(self, token: str = "access-token") -> None:
        self.token = token
        self.refreshes = 0

    async def refresh(self) -> None:
        self.refreshes += 1

    async def access_token(self) -> str:
        return self.token


class Trace:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(self, event: str, payload: dict[str, object]) -> None:
        self.events.append((event, payload))


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def make_tools(
    handler: object, *, max_output_chars: int = 65_536, trace: Trace | None = None
) -> tuple[GoogleApiTools, Tokens, httpx.AsyncClient, Trace]:
    tokens = Tokens()
    sink = trace or Trace()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return (
        GoogleApiTools(
            tokens,
            expected_email="person@example.com",
            max_output_chars=max_output_chars,
            client=client,
            trace=sink,
        ),
        tokens,
        client,
        sink,
    )


def test_definitions_are_the_fixed_google_surface() -> None:
    tools = GoogleApiTools(
        Tokens(), expected_email="person@example.com", max_output_chars=100
    )

    assert [item["name"] for item in tools.definitions] == [
        "google_gmail_search",
        "google_gmail_read_thread",
        "google_gmail_read_message",
        "google_drive_search",
        "google_drive_metadata",
        "google_drive_read_text",
        "google_drive_export_text",
        "google_calendar_search",
        "google_calendar_list",
        "google_calendar_read",
        "google_calendar_create",
        "google_calendar_update",
        "google_gmail_send",
        "google_gmail_reply",
    ]
    assert all(item["type"] == "function" for item in tools.definitions)
    assert tools.definitions[0]["parameters"]["additionalProperties"] is False  # type: ignore[index]


def test_connect_requires_the_exact_account_and_disconnect_invalidates_writes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://gmail.googleapis.com/gmail/v1/users/me/profile"
        return httpx.Response(200, json={"emailAddress": "PERSON@example.com"})

    tools, tokens, client, trace = make_tools(handler)

    async def scenario() -> None:
        assert tools.status() == "Google: disconnected"
        assert await tools.connect() == "Connected Google account."
        assert tools.status() == "Google: connected"
        proposed = await tools.execute(
            "google_calendar_create",
            {
                "calendarId": "primary",
                "summary": "Dentist",
                "startTime": "2026-09-03T09:00:00+03:00",
                "endTime": "2026-09-03T10:00:00+03:00",
                "timeZone": "Asia/Amman",
            },
        )
        assert isinstance(proposed, ApprovalRequired)
        assert tools.disconnect() == "Disconnected Google account."
        assert json.loads(await tools.resume(proposed.continuation, approved=True)) == {
            "error": {"kind": "connection_changed"}
        }
        assert tokens.refreshes == 1
        await client.aclose()

    run(scenario())
    assert all("access-token" not in json.dumps(payload) for _, payload in trace.events)


def test_connect_rejects_a_different_email_without_binding() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"emailAddress": "someone-else@example.com"})

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="configured Google account"):
            await tools.connect()
        assert tools.status() == "Google: disconnected"
        await client.aclose()

    run(scenario())


def test_gmail_search_uses_official_endpoint_and_canonical_bounded_result() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        return httpx.Response(
            200,
            json={
                "threads": [{"id": "t1", "snippet": "hello"}],
                "nextPageToken": "ignored",
            },
        )

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        result = json.loads(
            await tools.execute(
                "google_gmail_search",
                {
                    "query": "from:alice@example.com",
                    "pageSize": 20,
                    "includeTrash": False,
                    "view": "THREAD_VIEW_MINIMAL",
                },
            )
        )
        assert result == {"result": {"threads": [{"id": "t1", "snippet": "hello"}]}}
        await client.aclose()

    run(scenario())
    query = parse_qs(urlparse(str(calls[-1].url)).query)
    assert calls[-1].method == "GET"
    assert calls[-1].url.host == "gmail.googleapis.com"
    assert calls[-1].url.path == "/gmail/v1/users/me/threads"
    assert query["maxResults"] == ["20"]
    assert query["includeSpamTrash"] == ["false"]


def test_gmail_search_treats_no_content_as_an_empty_collection() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        return httpx.Response(204)

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        result = json.loads(
            await tools.execute(
                "google_gmail_search",
                {
                    "query": "from:nobody@example.com",
                    "pageSize": 20,
                    "includeTrash": False,
                    "view": "THREAD_VIEW_MINIMAL",
                },
            )
        )
        assert result == {"result": {"threads": []}}
        single_resource = json.loads(
            await tools.execute(
                "google_gmail_read_message",
                {"messageId": "missing", "messageFormat": "PLAIN_TEXT"},
            )
        )
        assert single_resource == {"error": {"kind": "operation_failed"}}
        await client.aclose()

    run(scenario())


@pytest.mark.parametrize(
    ("tool_name", "arguments", "collection_key"),
    [
        (
            "google_drive_search",
            {
                "query": "name = 'missing'",
                "pageSize": 20,
                "excludeContentSnippets": True,
            },
            "files",
        ),
        ("google_calendar_search", {"query": "missing", "pageSize": 20}, "items"),
        (
            "google_calendar_list",
            {
                "calendarId": "primary",
                "startTime": "2026-09-01T00:00:00Z",
                "endTime": "2026-09-02T00:00:00Z",
                "pageSize": 20,
            },
            "items",
        ),
    ],
)
def test_drive_and_calendar_collection_reads_treat_no_content_as_empty(
    tool_name: str, arguments: dict[str, object], collection_key: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        return httpx.Response(204)

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        result = json.loads(await tools.execute(tool_name, arguments))
        assert result == {"result": {collection_key: []}}
        await client.aclose()

    run(scenario())


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "google_drive_metadata",
            {"fileId": "missing", "excludeContentSnippets": True},
        ),
        ("google_calendar_read", {"eventId": "missing", "calendarId": "primary"}),
    ],
)
def test_single_resource_reads_fail_closed_on_no_content(
    tool_name: str, arguments: dict[str, object]
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        return httpx.Response(204)

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        result = json.loads(await tools.execute(tool_name, arguments))
        assert result == {"error": {"kind": "operation_failed"}}
        await client.aclose()

    run(scenario())


def test_gmail_read_decodes_inline_text_and_never_attachment_data() -> None:
    encode = lambda text: base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "threadId": "t1",
                "snippet": "hello",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "headers": [
                        {"name": "Subject", "value": "Hello"},
                        {"name": "Bcc", "value": "secret@example.com"},
                    ],
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": encode("plain")}},
                        {
                            "mimeType": "text/html",
                            "body": {
                                "data": encode("<p>html</p><script>secret</script>")
                            },
                        },
                        {
                            "filename": "secret.txt",
                            "mimeType": "text/plain",
                            "body": {"data": encode("attachment")},
                        },
                    ],
                },
            },
        )

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        result = json.loads(
            await tools.execute(
                "google_gmail_read_message",
                {"messageId": "m1", "messageFormat": "PLAIN_TEXT"},
            )
        )
        item = result["result"]
        assert item["body"] == "plain\n\nhtml"
        assert item["headers"] == {"Subject": "Hello"}
        assert "Bcc" not in item["headers"]
        assert "attachment" not in json.dumps(result)
        await client.aclose()

    run(scenario())


def test_drive_native_read_exports_and_plain_read_uses_media() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        if request.url.path.endswith("/files/doc"):
            return httpx.Response(
                200,
                json={"id": "doc", "mimeType": "application/vnd.google-apps.document"},
            )
        if request.url.path.endswith("/files/doc/export"):
            return httpx.Response(
                200, content=b"native text", headers={"content-type": "text/plain"}
            )
        if (
            request.url.path.endswith("/files/plain")
            and request.url.params.get("alt") != "media"
        ):
            return httpx.Response(200, json={"id": "plain", "mimeType": "text/plain"})
        return httpx.Response(
            200, content=b"plain text", headers={"content-type": "text/plain"}
        )

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        native = json.loads(
            await tools.execute(
                "google_drive_read_text", {"fileId": "doc", "includeComments": False}
            )
        )
        plain = json.loads(
            await tools.execute(
                "google_drive_read_text", {"fileId": "plain", "includeComments": False}
            )
        )
        assert native == {"result": {"content": "native text", "fileId": "doc"}}
        assert plain == {"result": {"content": "plain text", "fileId": "plain"}}
        await client.aclose()

    run(scenario())
    assert calls[-2].url.path.endswith("/files/plain")
    assert parse_qs(calls[-1].url.query.decode()) == {"alt": ["media"]}


def test_calendar_search_is_bounded_and_update_requires_a_changed_field() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        return httpx.Response(200, json={"items": []})

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        await tools.execute(
            "google_calendar_search", {"query": "dentist", "pageSize": 20}
        )
        with pytest.raises(ValueError, match="changed field"):
            await tools.execute(
                "google_calendar_update", {"calendarId": "primary", "eventId": "e1"}
            )
        await client.aclose()

    run(scenario())
    query = parse_qs(calls[-1].url.query.decode())
    assert query["maxResults"] == ["20"]
    assert httpx.QueryParams(query)["timeMax"] and httpx.QueryParams(query)["timeMin"]


def test_send_freezes_arguments_and_posts_one_rfc822_message() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        return httpx.Response(
            200,
            json={
                "id": "sent",
                "threadId": "new-thread",
                "labelIds": ["SENT"],
                "payload": {"body": "x" * 1000},
            },
        )

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        args = {
            "to": ["recipient@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "Subject",
            "body": "Hello",
            "mimeType": "text/plain",
        }
        proposed = await tools.execute("google_gmail_send", args)
        assert isinstance(proposed, ApprovalRequired)
        assert proposed.action.allow_save_permission is False
        args["subject"] = "changed"
        result = json.loads(await tools.resume(proposed.continuation, approved=True))
        assert result == {"result": {"id": "sent", "threadId": "new-thread"}}
        replay = json.loads(await tools.resume(proposed.continuation, approved=True))
        assert replay == {"error": {"kind": "already_resolved"}}
        await client.aclose()

    run(scenario())
    assert len(calls) == 2
    envelope = json.loads(calls[-1].content)
    raw = base64.urlsafe_b64decode(envelope["raw"] + "===")
    message = message_from_bytes(raw, policy=SMTP)
    assert message["To"] == "recipient@example.com"
    assert message["Subject"] == "Subject"
    assert message.get_content().strip() == "Hello"


def test_rejected_write_does_not_make_a_provider_post() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        return httpx.Response(200, json={"id": "never-used"})

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        proposed = await tools.execute(
            "google_calendar_create",
            {
                "calendarId": "primary",
                "summary": "Dentist",
                "startTime": "2026-09-03T09:00:00+03:00",
                "endTime": "2026-09-03T10:00:00+03:00",
                "timeZone": "Asia/Amman",
            },
        )
        assert isinstance(proposed, ApprovalRequired)
        assert json.loads(
            await tools.resume(proposed.continuation, approved=False)
        ) == {"rejected": True}
        await client.aclose()

    run(scenario())
    assert len(calls) == 1


def test_failed_write_is_ambiguous_after_exactly_one_post() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        return httpx.Response(503, json={"error": "try again"})

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        proposed = await tools.execute(
            "google_calendar_create",
            {
                "calendarId": "primary",
                "summary": "Dentist",
                "startTime": "2026-09-03T09:00:00+03:00",
                "endTime": "2026-09-03T10:00:00+03:00",
                "timeZone": "Asia/Amman",
            },
        )
        assert isinstance(proposed, ApprovalRequired)
        assert json.loads(await tools.resume(proposed.continuation, approved=True)) == {
            "error": {"kind": "outcome_ambiguous"}
        }
        await client.aclose()

    run(scenario())
    assert len(calls) == 2
    assert calls[-1].method == "POST"


def test_calendar_update_rejects_partial_time_changes_before_approval() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"emailAddress": "person@example.com"})

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        with pytest.raises(ValueError, match="startTime, endTime, and timeZone"):
            await tools.execute(
                "google_calendar_update",
                {
                    "calendarId": "primary",
                    "eventId": "event",
                    "startTime": "2026-09-03T09:00:00+03:00",
                },
            )
        await client.aclose()

    run(scenario())


def test_calendar_writes_reject_reversed_times_before_approval() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"emailAddress": "person@example.com"})

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        with pytest.raises(ValueError, match="start before end"):
            await tools.execute(
                "google_calendar_create",
                {
                    "calendarId": "primary",
                    "summary": "Invalid",
                    "startTime": "2026-09-03T10:00:00+03:00",
                    "endTime": "2026-09-03T09:00:00+03:00",
                    "timeZone": "Asia/Amman",
                },
            )
        await client.aclose()

    run(scenario())


def test_reply_fetches_source_metadata_before_approval_and_freezes_thread_headers() -> (
    None
):
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        if request.url.path.endswith("/messages/source"):
            return httpx.Response(
                200,
                json={
                    "id": "source",
                    "threadId": "thread",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "alice@example.com"},
                            {"name": "Subject", "value": "Question"},
                            {"name": "Message-ID", "value": "<source@example.com>"},
                            {"name": "References", "value": "<root@example.com>"},
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"id": "sent", "threadId": "thread"})

    tools, _, client, _ = make_tools(handler)

    async def scenario() -> None:
        await tools.connect()
        proposed = await tools.execute(
            "google_gmail_reply", {"messageId": "source", "body": "Answer"}
        )
        assert isinstance(proposed, ApprovalRequired)
        result = json.loads(await tools.resume(proposed.continuation, approved=True))
        assert result["result"]["threadId"] == "thread"
        await client.aclose()

    run(scenario())
    assert calls[1].url.path.endswith("/messages/source")
    envelope = json.loads(calls[2].content)
    message = message_from_bytes(
        base64.urlsafe_b64decode(envelope["raw"] + "==="), policy=SMTP
    )
    assert message["To"] == "alice@example.com"
    assert message["Subject"] == "Re: Question"
    assert message["In-Reply-To"] == "<source@example.com>"
    assert message["References"] == "<root@example.com> <source@example.com>"
    assert envelope["threadId"] == "thread"


def test_reads_fail_with_output_too_large_instead_of_truncating() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        return httpx.Response(200, json={"items": [{"value": "x" * 100}]})

    tools, _, client, _ = make_tools(handler, max_output_chars=40)

    async def scenario() -> None:
        await tools.connect()
        result = json.loads(
            await tools.execute(
                "google_calendar_read", {"eventId": "e1", "calendarId": "primary"}
            )
        )
        assert result == {"error": {"kind": "output_too_large"}}
        await client.aclose()

    run(scenario())
