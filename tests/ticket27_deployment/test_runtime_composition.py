"""Runtime composition and admission contract tests for Ticket 27."""

from __future__ import annotations

import stat
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from jarvis_control_plane.models import InboundMessage, SignedInboundEvent
from jarvis_control_plane.ports import ActionCancellationStatus
from jarvis_control_plane.service_runtime import (
    SERVICE_ROLES,
    CompositionError,
    _AsyncIngressAdmission,
    _GoogleActionDispatcher,
    _orchestration_operations,
    _read_secret,
    _service_access,
)
from jarvis_control_plane.sessions import SQLiteWorkingSessionStore


def test_orchestration_composition_uses_the_model_turn_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    captured: dict[str, object] = {}
    monkeypatch.setattr(runtime, "_credential_json", lambda _path: {"api_key": "key"})
    monkeypatch.setattr(runtime, "_client", lambda *_args, **_kwargs: object())

    def orchestration(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            run=lambda _request: None,
            cancel=lambda *, request_id: request_id == "request-01",
        )

    monkeypatch.setattr(runtime, "AgentsSdkOrchestrationAdapter", orchestration)
    operations = _orchestration_operations(
        {
            "timeouts": {"model_turn_seconds": 90},
        }
    )

    assert operations.keys() == {"run", "cancel"}
    assert captured["model_turn_timeout_seconds"] == 90
    identities, allowlists = _service_access(
        "orchestration_agent", SERVICE_ROLES["orchestration_agent"]
    )
    assert identities == ("jarvis-broker",)
    assert allowlists["jarvis-broker"] == ("run", "cancel")


def test_working_session_store_is_usable_from_service_handler_threads(
    tmp_path: Path,
) -> None:
    store = SQLiteWorkingSessionStore(tmp_path / "sessions.sqlite3")
    results: list[object] = []

    thread = Thread(target=lambda: results.append(store.load()))
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert results == [None]


def test_durable_ingress_admission_returns_before_background_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    dispatch_started = Event()
    release_dispatch = Event()

    class Receiver:
        def admit(self, _event: object) -> object:
            return SimpleNamespace(disposition="admitted", status_code=202)

    class Worker:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def run_once(self) -> object | None:
            self.calls += 1
            if self.calls == 1:
                dispatch_started.set()
                assert release_dispatch.wait(timeout=2)
                return SimpleNamespace(disposition="dispatched")
            return None

    monkeypatch.setattr(runtime, "OpenWAIngressWorker", Worker)
    admission = _AsyncIngressAdmission(
        receiver=Receiver(),  # type: ignore[arg-type]
        state=object(),
    )

    result = admission.receive(
        SignedInboundEvent.from_message(
            InboundMessage(
                event_type="message.received",
                session_id="session-1",
                event_id="event-1",
                message_id="message-1",
                sender_id="operator",
                chat_id="operator",
                chat_type="direct",
                message_type="text",
                from_me=False,
                text="do slow work",
            ),
            b"unused-test-secret",
        )
    )

    assert result.status_code == 202
    assert dispatch_started.wait(timeout=1)
    release_dispatch.set()


def test_durable_ingress_dispatches_cancel_while_ordinary_work_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    ordinary_started = Event()
    release_ordinary = Event()
    cancel_dispatched = Event()
    cancel_finished = Event()
    dispatch_count = 0

    class Receiver:
        def admit(self, _event: object) -> object:
            return SimpleNamespace(disposition="admitted", status_code=202)

        def dispatch_admitted_message(self, message: InboundMessage) -> object:
            nonlocal dispatch_count
            if message.text == "/cancel":
                dispatch_count += 1
                cancel_dispatched.set()
            return SimpleNamespace(disposition="dispatched")

    class State:
        finish_attempts = 0

        def begin_ingress_dispatch(self, **_kwargs: object) -> bool:
            return True

        def finish_ingress_dispatch(self, **_kwargs: object) -> None:
            self.finish_attempts += 1
            if self.finish_attempts == 1:
                raise RuntimeError("temporary durable-state failure")
            cancel_finished.set()

    class Worker:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def run_once(self) -> object | None:
            self.calls += 1
            if self.calls == 1:
                ordinary_started.set()
                assert release_ordinary.wait(timeout=2)
                return SimpleNamespace(disposition="dispatched")
            return None

    def event(text: str, suffix: str) -> SignedInboundEvent:
        return SignedInboundEvent.from_message(
            InboundMessage(
                event_type="message.received",
                session_id="session-1",
                event_id=f"event-{suffix}",
                message_id=f"message-{suffix}",
                sender_id="operator",
                chat_id="operator",
                chat_type="direct",
                message_type="text",
                from_me=False,
                text=text,
            ),
            b"unused-test-secret",
        )

    monkeypatch.setattr(runtime, "OpenWAIngressWorker", Worker)
    admission = _AsyncIngressAdmission(
        receiver=Receiver(),  # type: ignore[arg-type]
        state=State(),  # type: ignore[arg-type]
    )

    admission.receive(event("do slow work", "ordinary"))
    assert ordinary_started.wait(timeout=1)
    admission.receive(event("/cancel", "cancel"))

    assert cancel_dispatched.wait(timeout=1)
    assert cancel_finished.wait(timeout=1)
    assert dispatch_count == 1
    release_ordinary.set()


def test_google_action_finalization_retires_owner() -> None:
    owner = SimpleNamespace(
        prepare=lambda _action: object(),
        cancel=lambda **_kwargs: None,
    )
    dispatcher = _GoogleActionDispatcher(gmail=owner)  # type: ignore[arg-type]
    action = SimpleNamespace(kind="gmail_send", action_id="action-1")

    dispatcher.prepare(action)  # type: ignore[arg-type]
    dispatcher.finalize(action_id="action-1")

    assert (
        dispatcher.cancel(action_id="action-1").status
        is ActionCancellationStatus.UNKNOWN
    )


def test_protocol_keys_require_root_ownership_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    path = SimpleNamespace(
        stat=lambda: SimpleNamespace(st_mode=0o440, st_gid=20000, st_uid=10010),
        read_bytes=lambda: b"a" * 32,
        is_symlink=lambda: False,
    )
    monkeypatch.setattr(runtime.os, "name", "posix")

    with pytest.raises(CompositionError, match="ownership or mode"):
        _read_secret(path)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mode", "uid"),
    [
        (stat.S_IFREG | 0o640, 10008),
        (stat.S_IFREG | 0o600, 10009),
        (stat.S_IFLNK | 0o600, 10008),
    ],
)
def test_worker_private_key_requires_service_ownership_and_mode(
    monkeypatch: pytest.MonkeyPatch, mode: int, uid: int
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    path = SimpleNamespace(
        lstat=lambda: SimpleNamespace(st_mode=mode, st_uid=uid),
    )
    monkeypatch.setattr(runtime.os, "name", "posix")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 10008, raising=False)

    with pytest.raises(CompositionError, match="private key"):
        runtime._private_key_path(path)


def test_worker_runtime_requires_the_reviewed_overlay_port() -> None:
    import jarvis_control_plane.service_runtime as runtime

    with pytest.raises(CompositionError, match="9443"):
        runtime._reviewed_windows_overlay_port(9444)
