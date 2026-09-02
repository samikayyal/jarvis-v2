"""Native HTTP composition root for the personal assistant runtime."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import ConfigError, LoadedRuntimeConfig, RuntimeConfig, load_runtime_config
from .mcp import (
    GoogleConnectionManager,
    GoogleOAuthTokenProvider,
    HttpMcpTransport,
    McpManifestError,
    prepare_configured_mcp_services,
    validate_configured_mcp_manifests,
)
from .openwa import (
    OpenWASettings,
    WebhookAcknowledgement,
    build_openwa_message_flow,
)
from .responses import build_direct_responses_runner
from .runtime import build_runtime_from_loaded
from .trace import build_runtime_trace

MAX_HEADER_BYTES = 16 * 1024
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
LOGGER = logging.getLogger(__name__)


class WebhookFlow(Protocol):
    def receive_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> WebhookAcknowledgement: ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes = b""


class WebhookHttpApplication:
    """Expose only the private OpenWA webhook handoff."""

    def __init__(self, flow: WebhookFlow) -> None:
        self._flow = flow

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> HttpResponse:
        if target != "/webhook":
            return HttpResponse(404)
        if method != "POST":
            return HttpResponse(405)
        acknowledgement = self._flow.receive_webhook(body, headers)
        return HttpResponse(acknowledgement.status_code)


def validate_service_config(root: str | Path) -> RuntimeConfig:
    """Validate the service configuration without writes or network I/O."""

    loaded = _load_service_config(root)
    return loaded.config


def _load_service_config(root: str | Path) -> LoadedRuntimeConfig:
    loaded = load_runtime_config(root)
    if loaded.config.listener_host is None or loaded.config.listener_port is None:
        raise ConfigError(
            loaded.config.root / "jarvis.toml",
            "listener_host and listener_port must be configured for the service",
        )
    OpenWASettings.from_loaded_config(loaded)
    try:
        validate_configured_mcp_manifests(loaded.config.mcp_services)
    except McpManifestError as exc:
        raise ConfigError(loaded.config.root / "jarvis.toml", str(exc)) from exc
    return loaded


async def build_service_async(
    root: str | Path,
) -> tuple[WebhookHttpApplication, RuntimeConfig]:
    """Compose the single replacement service from its runtime directory."""

    loaded = _load_service_config(root)
    config = loaded.config
    trace = build_runtime_trace(config)
    configured_services = ()
    connections = None
    if config.mcp_services:
        client_id = loaded.secrets.google_oauth_client_id
        client_secret = loaded.secrets.google_oauth_client_secret
        refresh_token = loaded.secrets.google_oauth_refresh_token
        assert client_id and client_secret and refresh_token
        tokens = GoogleOAuthTokenProvider(client_id, client_secret, refresh_token)
        transport = HttpMcpTransport(
            tokens,
            authorized_endpoints={service.endpoint for service in config.mcp_services},
            trace=trace,
        )
        configured_services = await prepare_configured_mcp_services(
            config.mcp_services, transport
        )
        connections = GoogleConnectionManager(configured_services, tokens)
    runner = build_direct_responses_runner(
        loaded.secrets.openai_api_key,
        config,
        trace=trace,
        additional_tools=configured_services,
    )
    runtime = build_runtime_from_loaded(
        loaded,
        request_runner=runner,
        trace=trace,
        connections=connections,
    )
    flow = build_openwa_message_flow(loaded, runtime)
    return WebhookHttpApplication(flow), config


def build_service(root: str | Path) -> tuple[WebhookHttpApplication, RuntimeConfig]:
    """Compose the service outside an already running event loop."""

    return asyncio.run(build_service_async(root))


async def start_listener(
    application: WebhookHttpApplication, host: str, port: int
) -> asyncio.Server:
    """Start the bounded HTTP/1.1 listener on an already validated address."""

    return await asyncio.start_server(
        lambda reader, writer: _handle_connection(application, reader, writer),
        host,
        port,
        limit=MAX_HEADER_BYTES,
    )


async def run_service(root: str | Path) -> None:
    application, config = await build_service_async(root)
    assert config.listener_host is not None
    assert config.listener_port is not None
    server = await start_listener(
        application, config.listener_host, config.listener_port
    )
    LOGGER.info(
        "personal runtime listening on %s:%s",
        config.listener_host,
        config.listener_port,
    )
    async with server:
        await server.serve_forever()


async def _handle_connection(
    application: WebhookHttpApplication,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        request = await _read_request(reader)
        if isinstance(request, HttpResponse):
            response = request
        else:
            method, target, headers, body = request
            response = application.handle(method, target, headers, body)
    except Exception:
        LOGGER.exception("unhandled private webhook request error")
        response = HttpResponse(500)
    writer.write(_encode_response(response))
    try:
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | HttpResponse:
    try:
        header_block = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return HttpResponse(400)
    if len(header_block) > MAX_HEADER_BYTES:
        return HttpResponse(431)
    try:
        header_lines = header_block[:-4].decode("iso-8859-1").split("\r\n")
        method, target, version = header_lines[0].split(" ", 2)
    except (UnicodeDecodeError, ValueError):
        return HttpResponse(400)
    if version != "HTTP/1.1" or not method or not target:
        return HttpResponse(400)

    headers: dict[str, str] = {}
    content_lengths: list[str] = []
    for line in header_lines[1:]:
        if ":" not in line:
            return HttpResponse(400)
        name, value = line.split(":", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            return HttpResponse(400)
        if name.lower() == "content-length":
            content_lengths.append(value)
        if name.lower() == "transfer-encoding":
            return HttpResponse(400)
        headers[name] = value
    if len(content_lengths) != 1:
        return HttpResponse(411 if not content_lengths else 400)
    try:
        content_length = int(content_lengths[0])
    except ValueError:
        return HttpResponse(400)
    if content_length < 0 or content_length > MAX_WEBHOOK_BODY_BYTES:
        return HttpResponse(413)
    try:
        body = await reader.readexactly(content_length)
    except asyncio.IncompleteReadError:
        return HttpResponse(400)
    return method, target, headers, body


_REASONS = {
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Content Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
}


def _encode_response(response: HttpResponse) -> bytes:
    reason = _REASONS.get(response.status_code, "Unknown")
    return (
        f"HTTP/1.1 {response.status_code} {reason}\r\n"
        f"Content-Length: {len(response.body)}\r\n"
        "Content-Type: application/octet-stream\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + response.body


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Jarvis personal runtime")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration without binding or contacting dependencies",
    )
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if arguments.check:
            validate_service_config(arguments.root)
            return 0
        asyncio.run(run_service(arguments.root))
    except KeyboardInterrupt:
        return 0
    except Exception:
        LOGGER.exception("personal runtime stopped")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the script
    raise SystemExit(main())


__all__ = [
    "HttpResponse",
    "WebhookHttpApplication",
    "build_service",
    "build_service_async",
    "main",
    "run_service",
    "start_listener",
    "validate_service_config",
]
