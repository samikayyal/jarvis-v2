from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis_personal_runtime.config import ConfigError
from jarvis_personal_runtime.openwa import (
    WebhookAcknowledgement,
    WebhookDisposition,
)
from jarvis_personal_runtime.service import (
    WebhookHttpApplication,
    build_service_async,
    start_listener,
    validate_service_config,
)


class _Flow:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, dict[str, str]]] = []

    def receive_webhook(
        self, raw_body: bytes, headers: dict[str, str]
    ) -> WebhookAcknowledgement:
        self.calls.append((raw_body, headers))
        return WebhookAcknowledgement(202, WebhookDisposition.ADMITTED)


def test_http_application_exposes_only_the_content_free_webhook() -> None:
    flow = _Flow()
    app = WebhookHttpApplication(flow)

    response = app.handle(
        "POST", "/webhook", {"X-OpenWA-Signature": "sha256=test"}, b'{"x":1}'
    )

    assert response.status_code == 202
    assert response.body == b""
    assert flow.calls == [(b'{"x":1}', {"X-OpenWA-Signature": "sha256=test"})]
    assert app.handle("GET", "/webhook", {}, b"").status_code == 405
    assert app.handle("POST", "/status", {}, b"").status_code == 404
    assert len(flow.calls) == 1


def test_listener_returns_the_admission_acknowledgement_over_http() -> None:
    async def scenario() -> None:
        flow = _Flow()
        server = await start_listener(WebhookHttpApplication(flow), "127.0.0.1", 0)
        try:
            socket = server.sockets[0]
            host, port = socket.getsockname()[:2]
            reader, writer = await asyncio.open_connection(host, port)
            body = b'{"event":"message.received"}'
            writer.write(
                b"POST /webhook HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"X-OpenWA-Signature: sha256=test\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"\r\n"
                + body
            )
            await writer.drain()

            response = await reader.read()

            assert response.startswith(b"HTTP/1.1 202 Accepted\r\n")
            assert b"Content-Length: 0\r\n" in response
            assert flow.calls[0][0] == body
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def _write_service_config(root: Path, *, include_listener: bool = True) -> None:
    root.mkdir()
    (root / ".env").write_text(
        "OPENAI_API_KEY=sk-test\n"
        "OPENWA_API_KEY=openwa-test\n"
        "OPENWA_WEBHOOK_SIGNING_SECRET=signing-test\n",
        encoding="utf-8",
    )
    listener = (
        'listener_host = "172.17.0.1"\nlistener_port = 8787\n'
        if include_listener
        else ""
    )
    (root / "jarvis.toml").write_text(
        "[runtime]\n"
        + listener
        + 'openwa_api_base_url = "http://172.17.0.2:2785/api"\n'
        + 'openwa_internal_session_id = "internal-session"\n'
        + 'openwa_named_session = "jarvis"\n'
        + 'openwa_authorized_operator_number = "962790000000@c.us"\n'
        + 'openwa_operator_chat_id = "962790000000@c.us"\n',
        encoding="utf-8",
    )
    (root / "SYSTEM.md").write_text("You are Jarvis.\n", encoding="utf-8")


def test_service_check_validates_without_writing_or_contacting_dependencies(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    _write_service_config(root)
    before = {path.name: path.read_bytes() for path in root.iterdir()}

    config = validate_service_config(root)

    assert config.listener_host == "172.17.0.1"
    assert config.listener_port == 8787
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before


def test_service_check_requires_an_explicit_private_listener(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _write_service_config(root, include_listener=False)

    with pytest.raises(ConfigError, match="listener_host and listener_port"):
        validate_service_config(root)


def test_async_service_composition_can_prepare_configured_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    _write_service_config(root)
    (root / ".env").write_text(
        (root / ".env").read_text(encoding="utf-8")
        + "GOOGLE_OAUTH_CLIENT_ID=client\n"
        + "GOOGLE_OAUTH_CLIENT_SECRET=secret\n"
        + "GOOGLE_OAUTH_REFRESH_TOKEN=refresh\n",
        encoding="utf-8",
    )
    manifest = root / "calendar.json"
    manifest.write_text(
        (
            Path(__file__).resolve().parents[2]
            / "deployment/personal-runtime/manifests/google-calendar.json"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with (root / "jarvis.toml").open("a", encoding="utf-8") as output:
        output.write(
            '\n[[mcp_services]]\nid = "google-calendar"\n'
            'endpoint = "https://calendarmcp.googleapis.com/mcp/v1"\n'
            'manifest_path = "calendar.json"\nmax_output_chars = 20000\n'
        )

    async def prepared(configs: object, transport: object) -> tuple[object, ...]:
        return ()

    monkeypatch.setattr(
        "jarvis_personal_runtime.service.prepare_configured_mcp_services", prepared
    )

    application, config = asyncio.run(build_service_async(root))

    assert isinstance(application, WebhookHttpApplication)
    assert len(config.mcp_services) == 1
