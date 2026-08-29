"""Administrative status contract tests for Ticket 27."""

from __future__ import annotations

import json
import tomllib
from types import SimpleNamespace

import pytest

from jarvis_control_plane.deployment import (
    RESOURCE_LIMITS,
    verify_bundle,
)
from jarvis_control_plane.deployment import (
    administrative_status as deployment_administrative_status,
)
from jarvis_control_plane.openwa import OpenWAReadiness
from jarvis_control_plane.ports import WorkerReadiness
from jarvis_control_plane.service_runtime import (
    administrative_status as service_administrative_status,
)

from .helpers import REPOSITORY_ROOT, SHIPPED_BUNDLE


@pytest.mark.parametrize("compose_json_lines", (False, True))
def test_administrative_status_reports_safe_operational_state(
    monkeypatch: pytest.MonkeyPatch,
    compose_json_lines: bool,
) -> None:
    config = tomllib.loads(
        (SHIPPED_BUNDLE / "config.example.toml").read_text(encoding="utf-8")
    )

    class Client:
        def __init__(self, role: str) -> None:
            self.role = role

        def call(self, operation: str) -> object:
            if self.role == "audit_service":
                assert operation == "writable"
                return True
            assert operation == "current"
            if self.role == "openwa_outbound_connector":
                return OpenWAReadiness(True, "ready")
            return WorkerReadiness(ubuntu="ready", windows="unavailable")

    monkeypatch.setattr(
        "jarvis_control_plane.service_runtime._client",
        lambda _config, *, client_identity, server_role: Client(server_role),
    )
    monkeypatch.setattr(
        "jarvis_control_plane.service_runtime.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=3 * 1024**3),
    )

    dependency_status = service_administrative_status(
        config,
        artifact_lock_path=SHIPPED_BUNDLE / "artifacts.lock.json",
    )

    services = verify_bundle(SHIPPED_BUNDLE, source_root=REPOSITORY_ROOT).services
    calls = 0
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        commands.append(command)
        if calls == 1:
            rows = [
                {"Service": service, "State": "running", "Health": "healthy"}
                for service in services
            ]
            return SimpleNamespace(
                stdout=(
                    "\n".join(json.dumps(row) for row in rows)
                    if compose_json_lines
                    else json.dumps(rows)
                )
            )
        return SimpleNamespace(stdout=json.dumps(dependency_status))

    override = SHIPPED_BUNDLE / "activation.compose.example.yaml"
    status = deployment_administrative_status(
        SHIPPED_BUNDLE, activation_override=override, runner=run
    )

    assert set(status) == {
        "components",
        "messaging_ready",
        "audit_writable",
        "backup_freshness",
        "hosts",
        "release",
        "resource_pressure",
    }
    assert set(status["components"].values()) == {"ready"}
    assert status["messaging_ready"] is True
    assert status["hosts"] == {"ubuntu": "ready", "windows": "unavailable"}
    assert status["audit_writable"] is True
    assert status["backup_freshness"] == "missing"
    assert status["resource_pressure"] == "ok"
    expected_prefix = [
        "docker",
        "compose",
        "--file",
        str((SHIPPED_BUNDLE / "compose.yaml").resolve()),
        "--file",
        str(override.resolve()),
        "--profile",
        "manual-activation",
    ]
    assert commands[0][:-4] == expected_prefix
    assert commands[0][-4:] == ["ps", "--all", "--format", "json"]
    assert commands[1] == [
        *expected_prefix,
        "exec",
        "--interactive=false",
        "-T",
        "capability_broker",
        "uv",
        "run",
        "--no-project",
        "python",
        "-m",
        "jarvis_control_plane.service_runtime",
        "admin-status",
    ]


@pytest.mark.parametrize(
    "rows",
    (
        [],
        ["not-an-object"],
        [
            {"Service": service, "State": "running", "Health": "healthy"}
            for service in tuple(RESOURCE_LIMITS)[:-1]
        ],
        [
            *(
                {"Service": service, "State": "running", "Health": "healthy"}
                for service in RESOURCE_LIMITS
            ),
            {"Service": next(iter(RESOURCE_LIMITS)), "State": "running"},
        ],
        [
            {"Service": "unknown", "State": "running", "Health": "healthy"},
            *(
                {"Service": service, "State": "running", "Health": "healthy"}
                for service in tuple(RESOURCE_LIMITS)[1:]
            ),
        ],
    ),
    ids=("empty", "non-object", "missing", "duplicate", "unknown"),
)
def test_administrative_status_rejects_ambiguous_compose_inventory(
    rows: list[object],
) -> None:
    calls = 0

    def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(stdout=json.dumps(rows))

    with pytest.raises(RuntimeError, match="administrative status is unavailable"):
        deployment_administrative_status(
            SHIPPED_BUNDLE,
            activation_override=SHIPPED_BUNDLE / "activation.compose.example.yaml",
            runner=run,
        )
    assert calls == 1
