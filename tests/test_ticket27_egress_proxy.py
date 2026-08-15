"""Ticket 27 closed egress proxy tests."""

from __future__ import annotations

import socket
from io import BytesIO
from threading import Thread
from time import monotonic, sleep

import pytest

from jarvis_control_plane.egress_proxy import (
    _connect_target,
    _read_headers,
    main,
    serve_egress_proxy,
)
from jarvis_control_plane.service_protocol import find_available_port


def _connect_request(authority: str) -> bytes:
    return (f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}").encode("ascii")


def test_connect_proxy_admits_only_the_exact_host_and_port_allowlist() -> None:
    assert _connect_target(
        _connect_request("api.openai.com:443"),
        allowed_hosts=frozenset({"api.openai.com"}),
        allowed_ports=frozenset({443}),
    ) == ("api.openai.com", 443)

    for authority in (
        "attacker.invalid:443",
        "api.openai.com:22",
        "api.openai.com@attacker.invalid:443",
    ):
        with pytest.raises((PermissionError, ValueError)):
            _connect_target(
                _connect_request(authority),
                allowed_hosts=frozenset({"api.openai.com"}),
                allowed_ports=frozenset({443}),
            )


def test_proxy_health_does_not_require_an_external_connection() -> None:
    port = find_available_port()
    Thread(
        target=serve_egress_proxy,
        kwargs={
            "host": "127.0.0.1",
            "port": port,
            "allowed_hosts": ("api.openai.com",),
            "allowed_ports": (443,),
        },
        daemon=True,
    ).start()
    deadline = monotonic() + 2
    while True:
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=2)
            break
        except OSError:
            if monotonic() >= deadline:
                raise
            sleep(0.01)
    with connection:
        connection.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert connection.recv(128).startswith(b"HTTP/1.1 200 ")


def test_proxy_command_preserves_coalesced_tunnel_bytes() -> None:
    class Connection:
        def __init__(self) -> None:
            self.payload = BytesIO(
                b"HTTP/1.1 200 Connection Established\r\n\r\nSSH-2.0-upstream\r\n"
            )

        def recv(self, size: int) -> bytes:
            return self.payload.read(size)

    head, remainder = _read_headers(Connection(), allow_remainder=True)  # type: ignore[arg-type]

    assert head.startswith(b"HTTP/1.1 200 ")
    assert remainder == b"SSH-2.0-upstream\r\n"


def test_lightweight_proxy_command_parses_the_reviewed_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "jarvis_control_plane.egress_proxy.connect_through_proxy",
        lambda **kwargs: captured.update(kwargs),
    )

    assert main(["github.com", "22", "--proxy-host", "vault_egress_proxy"]) == 0
    assert captured == {
        "proxy_host": "vault_egress_proxy",
        "proxy_port": 9080,
        "target_host": "github.com",
        "target_port": 22,
    }
