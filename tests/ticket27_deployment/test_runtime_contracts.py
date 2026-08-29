"""Runtime boundary and storage contract tests for Ticket 27."""

from __future__ import annotations

import runpy
import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis_control_plane.models import SignedInboundEvent
from jarvis_control_plane.ports import TraceCapacityError
from jarvis_control_plane.service_runtime import (
    CompositionError,
    _broker_state_store,
    _load_configuration,
    _operation_timeouts,
    _vault_write_timeout,
    _verified_inbound_event,
)
from jarvis_control_plane.traces import SQLiteDiagnosticTraceStore

from .helpers import REPOSITORY_ROOT, SHIPPED_BUNDLE, _active_configuration


@pytest.fixture(autouse=True)
def _use_original_test_module_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        globals(),
        "__file__",
        str(REPOSITORY_ROOT / "tests" / "test_ticket27_deployment_bundle.py"),
    )


def test_broker_state_uses_only_the_write_only_deleted_archive_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    archive = object()
    captured: dict[str, object] = {}
    monkeypatch.setattr(runtime, "_read_secret", lambda _path: b"a" * 32)
    monkeypatch.setattr(
        runtime,
        "SQLiteDeletedConversationArchiveWriter",
        lambda endpoint, **kwargs: (
            captured.update(endpoint=endpoint, **kwargs) or archive
        ),
    )
    monkeypatch.setattr(
        runtime,
        "SQLiteDurableStateStore",
        lambda database, **kwargs: (
            captured.update(database=database, **kwargs) or object()
        ),
    )

    _broker_state_store(tmp_path)

    assert captured["endpoint"] == "/run/jarvis-deleted/writer.sock"
    assert captured["deleted_archive"] is archive
    assert captured["database"] == tmp_path / "state.sqlite3"


def test_deleted_archive_health_probe_does_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health_probe = runpy.run_path(
        str(Path(__file__).parents[1] / "deployment" / "health_probe.py")
    )

    monkeypatch.setattr(
        health_probe["os"],
        "stat",
        lambda _endpoint: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660),
    )
    monkeypatch.setattr(
        health_probe["socket"],
        "socket",
        lambda *_args, **_kwargs: pytest.fail("health probe must not connect"),
    )

    health_probe["deleted_archive_ready"]()


def test_service_transport_timeouts_cover_configured_operation_deadlines() -> None:
    config = tomllib.loads((SHIPPED_BUNDLE / "config.example.toml").read_text("utf-8"))
    assert _operation_timeouts(config, server_role="capability_broker")["receive"] > 480
    assert _operation_timeouts(config, server_role="orchestration_agent")["run"] > 480
    worker = _operation_timeouts(config, server_role="worker_gateway")
    assert set(worker.values()) == {145.0}


def test_vault_write_uses_the_configured_side_effect_budget() -> None:
    config = tomllib.loads((SHIPPED_BUNDLE / "config.example.toml").read_text("utf-8"))

    assert _vault_write_timeout(config) == 30.0


def test_trace_admission_preserves_the_configured_free_disk_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jarvis_control_plane.traces.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=150),
    )
    store = SQLiteDiagnosticTraceStore(
        tmp_path / "traces.sqlite3",
        capacity_bytes=1000,
        reservation_bytes=100,
        hard_max_bytes=100,
        minimum_free_bytes=100,
    )
    with pytest.raises(TraceCapacityError) as raised:
        store.reserve(request_id="request-low-disk", reservation_bytes=51)
    assert raised.value.available_bytes == 50


def test_runtime_rejects_unknown_active_configuration_before_binding(
    tmp_path: Path,
) -> None:
    path = _active_configuration(tmp_path)
    path.chmod(0o644)
    path.write_text(f"unexpected = true\n{path.read_text('utf-8')}", encoding="utf-8")
    path.chmod(0o444)

    with pytest.raises(CompositionError, match="failed validation"):
        _load_configuration(path)


def test_runtime_rejects_wrong_active_configuration_mode(tmp_path: Path) -> None:
    path = _active_configuration(tmp_path)
    path.chmod(0o644)

    with pytest.raises(CompositionError, match="mode 0444"):
        _load_configuration(path)


def test_inbound_receiver_verifies_raw_body_before_forwarding() -> None:
    secret = b"receiver-scoped-openwa-signing-secret"
    signed = SignedInboundEvent.from_mapping({"message": "exact"}, secret)

    assert _verified_inbound_event(signed.raw_body, signed.signature, secret) == signed
    assert (
        _verified_inbound_event(signed.raw_body + b" ", signed.signature, secret)
        is None
    )
    assert _verified_inbound_event(signed.raw_body, None, secret) is None
