"""Small CONNECT-only egress boundary for credential-bearing services."""

from __future__ import annotations

import argparse
import select
import socket
import sys
from collections.abc import Collection
from socketserver import BaseRequestHandler, ThreadingTCPServer
from threading import Thread
from urllib.parse import urlsplit

MAX_PROXY_HEADER_BYTES = 8192
_PROXY_CHUNK_BYTES = 64 * 1024


def _read_available(stream: object) -> bytes:
    """Read one currently available pipe chunk without waiting to fill the buffer."""

    return stream.read1(_PROXY_CHUNK_BYTES)  # type: ignore[attr-defined,no-any-return]


def _read_headers(
    connection: socket.socket, *, allow_remainder: bool = False
) -> tuple[bytes, bytes]:
    payload = bytearray()
    while b"\r\n\r\n" not in payload:
        chunk = connection.recv(1024)
        if not chunk:
            raise OSError("egress proxy client disconnected")
        payload.extend(chunk)
        if len(payload) > MAX_PROXY_HEADER_BYTES:
            raise ValueError("egress proxy headers are oversized")
    head, remainder = bytes(payload).split(b"\r\n\r\n", 1)
    if remainder and not allow_remainder:
        raise ValueError("egress proxy CONNECT request contained a body")
    return head, remainder


def _connect_target(
    request_head: bytes,
    *,
    allowed_hosts: frozenset[str],
    allowed_ports: frozenset[int],
) -> tuple[str, int]:
    try:
        lines = request_head.decode("ascii").split("\r\n")
        method, authority, version = lines[0].split(" ")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("egress proxy request line is invalid") from exc
    if method != "CONNECT" or version != "HTTP/1.1":
        raise PermissionError("egress proxy permits CONNECT only")
    parsed = urlsplit(f"//{authority}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("egress proxy target port is invalid") from exc
    host = parsed.hostname
    if (
        host is None
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("egress proxy target is invalid")
    canonical_host = host.encode("idna").decode("ascii").lower()
    if canonical_host not in allowed_hosts or port not in allowed_ports:
        raise PermissionError("egress proxy target is outside the active allowlist")
    return canonical_host, port


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    peers = (client, upstream)
    while True:
        readable, _writable, _exceptional = select.select(peers, (), peers, 600)
        if not readable:
            return
        for source in readable:
            data = source.recv(64 * 1024)
            if not data:
                return
            (upstream if source is client else client).sendall(data)


def serve_egress_proxy(
    *,
    host: str,
    port: int,
    allowed_hosts: Collection[str],
    allowed_ports: Collection[int],
) -> None:
    canonical_hosts = frozenset(
        value.encode("idna").decode("ascii").lower() for value in allowed_hosts
    )
    canonical_ports = frozenset(allowed_ports)
    if not canonical_hosts or not canonical_ports:
        raise ValueError("egress proxy requires closed host and port allowlists")

    class Handler(BaseRequestHandler):
        def handle(self) -> None:
            client = self.request
            client.settimeout(10)
            try:
                request_head, _remainder = _read_headers(client)
                if (
                    request_head == b"GET /health HTTP/1.1\r\nHost: localhost"
                    and self.client_address[0] in {"127.0.0.1", "::1"}
                ):
                    client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                    return
                target_host, target_port = _connect_target(
                    request_head,
                    allowed_hosts=canonical_hosts,
                    allowed_ports=canonical_ports,
                )
                upstream = socket.create_connection(
                    (target_host, target_port), timeout=10
                )
            except PermissionError:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            except (OSError, ValueError):
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            with upstream:
                client.sendall(
                    b"HTTP/1.1 200 Connection Established\r\nContent-Length: 0\r\n\r\n"
                )
                client.settimeout(None)
                upstream.settimeout(None)
                _relay(client, upstream)

    class Server(ThreadingTCPServer):
        allow_reuse_address = False
        daemon_threads = True
        request_queue_size = 16

    with Server((host, port), Handler) as server:
        server.serve_forever()


def connect_through_proxy(
    *, proxy_host: str, proxy_port: int, target_host: str, target_port: int
) -> None:
    """Bridge OpenSSH ProxyCommand stdio through the reviewed CONNECT proxy."""

    with socket.create_connection((proxy_host, proxy_port), timeout=10) as connection:
        request = (
            f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            f"Host: {target_host}:{target_port}\r\n\r\n"
        ).encode("ascii")
        connection.sendall(request)
        response, remainder = _read_headers(connection, allow_remainder=True)
        if not response.startswith(b"HTTP/1.1 200 "):
            raise OSError("vault egress proxy rejected the configured remote")
        connection.settimeout(None)

        def upload() -> None:
            while chunk := _read_available(sys.stdin.buffer):
                connection.sendall(chunk)
            connection.shutdown(socket.SHUT_WR)

        uploader = Thread(target=upload, daemon=True)
        uploader.start()
        if remainder:
            sys.stdout.buffer.write(remainder)
            sys.stdout.buffer.flush()
        while chunk := connection.recv(_PROXY_CHUNK_BYTES):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        uploader.join(timeout=1)


def main(argv: list[str] | None = None) -> int:
    """Run the lightweight OpenSSH ProxyCommand without importing service roots."""

    parser = argparse.ArgumentParser()
    parser.add_argument("target_host")
    parser.add_argument("target_port", type=int)
    parser.add_argument("--proxy-host", required=True)
    parser.add_argument("--proxy-port", type=int, default=9080)
    arguments = parser.parse_args(argv)
    connect_through_proxy(
        proxy_host=arguments.proxy_host,
        proxy_port=arguments.proxy_port,
        target_host=arguments.target_host,
        target_port=arguments.target_port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
