from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from typing import Self
from urllib.request import Request

import pytest

from jarvis_control_plane.native_worker_runtime import (
    WindowsWorkerRuntimeConfig,
    load_ubuntu_config,
    run_windows_worker_loop,
)
from jarvis_control_plane.openwa_webhook import (
    TARGET_URL,
    WebhookProvisionError,
    provision_webhook,
)


class _Response:
    def __init__(self, value: object) -> None:
        self._raw = json.dumps(value).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def test_ubuntu_runtime_config_is_strict(tmp_path: Path) -> None:
    config = tmp_path / "ubuntu.json"
    config.write_text(
        json.dumps(
            {
                "worker_id": "ubuntu-native",
                "connection_id": "ubuntu-connection",
                "socket_path": "/run/jarvis-worker/ubuntu.sock",
                "gateway_uid": 10008,
                "process_limit": 32,
            }
        )
    )
    assert load_ubuntu_config(config).gateway_uid == 10008

    value = json.loads(config.read_text())
    value["unexpected"] = True
    config.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="schema mismatch"):
        load_ubuntu_config(config)


def _windows_config(tmp_path: Path) -> WindowsWorkerRuntimeConfig:
    return WindowsWorkerRuntimeConfig(
        worker_id="desktop-46nt3s7",
        connection_id="windows-connection",
        application_identity="jarvis-windows-worker/desktop-46nt3s7",
        overlay_host="100.106.206.88",
        overlay_port=9443,
        server_name="sami-lenovo.tailb09c76.ts.net",
        ca_file=tmp_path / "ca.pem",
        certificate_file=tmp_path / "cert.pem",
        private_key_file=tmp_path / "key.pem",
    )


def test_windows_loop_sends_closed_hello_and_stops(tmp_path: Path) -> None:
    stop = Event()
    calls: list[dict[str, object]] = []

    def client(**kwargs: object) -> None:
        calls.append(kwargs)
        stop.set()

    run_windows_worker_loop(_windows_config(tmp_path), stop, client=client)
    hello = json.loads(calls[0]["application_hello"])
    assert hello == {
        "application_identity": "jarvis-windows-worker/desktop-46nt3s7",
        "connection_id": "windows-connection",
        "heartbeat_interval_seconds": 10,
        "host": "windows",
        "worker_id": "desktop-46nt3s7",
    }


def test_webhook_provision_creates_and_verifies_without_secret_output() -> None:
    requests: list[Request] = []
    responses = iter(
        [
            [],
            {"id": "hook-1"},
            [
                {
                    "id": "hook-1",
                    "url": TARGET_URL,
                    "events": ["message.received"],
                    "active": True,
                    "retryCount": 3,
                }
            ],
        ]
    )

    def opener(request: Request, **_kwargs: object) -> _Response:
        requests.append(request)
        return _Response(next(responses))

    result = provision_webhook(
        api_base_url="http://127.0.0.1:2785/api",
        session_id="internal/session",
        api_key="api-super-secret",
        signing_secret="hmac-super-secret",
        opener=opener,
    )
    assert result == "created"
    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert requests[0].full_url.endswith("/internal%2Fsession/webhooks")
    assert requests[0].get_header("X-api-key") == "api-super-secret"
    assert b"hmac-super-secret" in (requests[1].data or b"")


def test_webhook_provision_fails_closed_on_competing_receiver() -> None:
    def opener(_request: Request, **_kwargs: object) -> _Response:
        return _Response(
            [
                {
                    "id": "other",
                    "url": "https://elsewhere.invalid/hook",
                    "events": ["message.received"],
                }
            ]
        )

    with pytest.raises(WebhookProvisionError, match="conflicting"):
        provision_webhook(
            api_base_url="http://127.0.0.1:2785/api",
            session_id="session",
            api_key="api-secret",
            signing_secret="hmac-secret",
            opener=opener,
        )


def test_deployment_contains_reviewed_native_service_definitions() -> None:
    root = Path(__file__).parents[1]
    unit = (root / "deployment/systemd/jarvis-ubuntu-worker.service").read_text()
    installer = (root / "deployment/windows/install-jarvis-worker.ps1").read_text()
    handoff = (root / "deployment/openwa-handoff.md").read_text()
    assert "User=jarvis-worker" in unit
    assert "XDG_RUNTIME_DIR=/run/user/10008" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "StartupType Manual" in installer
    assert "NT AUTHORITY\\LOCAL SERVICE" in installer
    assert "SSRF_ALLOWED_HOSTS" in handoff
