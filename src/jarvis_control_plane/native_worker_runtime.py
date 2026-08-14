"""Manually activated host runtimes for the two native Jarvis workers."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import socket
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event

from .ports import ActionDispatcherError
from .ubuntu_worker import (
    SystemdUbuntuProcessScope,
    UbuntuLocalPeerExpectation,
    UbuntuWorkerReadiness,
    UbuntuWorkerService,
    UnixSocketUbuntuLocalAuthenticator,
)
from .ubuntu_worker_ipc import serve_ubuntu_worker_connection
from .windows_worker import SubprocessWindowsJobObjectExecutor, WindowsMtlsClientConfig
from .windows_worker_session import run_windows_worker_client


@dataclass(frozen=True, slots=True)
class UbuntuWorkerRuntimeConfig:
    worker_id: str
    connection_id: str
    socket_path: str
    gateway_uid: int
    process_limit: int = 32

    def __post_init__(self) -> None:
        _canonical_text(self.worker_id, "Ubuntu worker ID")
        _canonical_text(self.connection_id, "Ubuntu connection ID")
        if not PurePosixPath(self.socket_path).is_absolute():
            raise ValueError("Ubuntu worker socket path must be absolute")
        if str(PurePosixPath(self.socket_path)) != self.socket_path:
            raise ValueError("Ubuntu worker socket path must be canonical")
        _bounded_int(self.gateway_uid, "Ubuntu gateway UID", 1, 2**31 - 1)
        _bounded_int(self.process_limit, "Ubuntu process limit", 1, 64)


@dataclass(frozen=True, slots=True)
class WindowsWorkerRuntimeConfig:
    worker_id: str
    connection_id: str
    application_identity: str
    overlay_host: str
    overlay_port: int
    server_name: str
    ca_file: Path
    certificate_file: Path
    private_key_file: Path
    heartbeat_interval_seconds: int = 10
    reconnect_seconds: int = 5
    process_limit: int = 32

    def __post_init__(self) -> None:
        for name in (
            "worker_id",
            "connection_id",
            "application_identity",
            "overlay_host",
            "server_name",
        ):
            _canonical_text(getattr(self, name), f"Windows {name.replace('_', ' ')}")
        _bounded_int(self.overlay_port, "Windows overlay port", 1, 65535)
        _bounded_int(
            self.heartbeat_interval_seconds, "Windows heartbeat interval", 1, 15
        )
        _bounded_int(self.reconnect_seconds, "Windows reconnect interval", 1, 60)
        _bounded_int(self.process_limit, "Windows process limit", 1, 64)
        for name in ("ca_file", "certificate_file", "private_key_file"):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise ValueError(f"Windows {name.replace('_', ' ')} must be absolute")
            object.__setattr__(self, name, value)

    @property
    def mtls(self) -> WindowsMtlsClientConfig:
        return WindowsMtlsClientConfig(
            overlay_host=self.overlay_host,
            overlay_port=self.overlay_port,
            server_name=self.server_name,
            ca_file=self.ca_file,
            certificate_file=self.certificate_file,
            private_key_file=self.private_key_file,
        )

    @property
    def hello(self) -> bytes:
        return json.dumps(
            {
                "host": "windows",
                "worker_id": self.worker_id,
                "connection_id": self.connection_id,
                "application_identity": self.application_identity,
                "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


def _canonical_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be canonical non-blank text")
    return value


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _load_object(path: str | Path) -> Mapping[str, object]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"native worker config is unreadable: {source}") from exc
    if not isinstance(value, dict):
        raise TypeError("native worker config must be a JSON object")
    return value


def load_ubuntu_config(path: str | Path) -> UbuntuWorkerRuntimeConfig:
    value = _load_object(path)
    required = {"worker_id", "connection_id", "socket_path", "gateway_uid", "process_limit"}
    if set(value) != required:
        raise ValueError("Ubuntu worker config schema mismatch")
    return UbuntuWorkerRuntimeConfig(**value)  # type: ignore[arg-type]


def load_windows_config(path: str | Path) -> WindowsWorkerRuntimeConfig:
    value = _load_object(path)
    required = {
        "worker_id", "connection_id", "application_identity", "overlay_host",
        "overlay_port", "server_name", "ca_file", "certificate_file",
        "private_key_file", "heartbeat_interval_seconds", "reconnect_seconds",
        "process_limit",
    }
    if set(value) != required:
        raise ValueError("Windows worker config schema mismatch")
    for name in ("ca_file", "certificate_file", "private_key_file"):
        value[name] = Path(value[name])  # type: ignore[arg-type]
    return WindowsWorkerRuntimeConfig(**value)  # type: ignore[arg-type]


def run_ubuntu_worker(config: UbuntuWorkerRuntimeConfig, stop: Event | None = None) -> None:
    if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("native Ubuntu worker requires Linux")
    stop = stop or Event()
    path = Path(config.socket_path)
    parent = path.parent
    if not parent.is_dir() or parent.resolve() != parent:
        raise RuntimeError("Ubuntu worker socket directory is not canonical")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not path.is_socket() or existing.st_uid != os.geteuid():
            raise RuntimeError("refusing to replace an untrusted Ubuntu worker socket")
        path.unlink()

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    process_scope = SystemdUbuntuProcessScope(process_limit=config.process_limit)
    try:
        listener.bind(config.socket_path)
        os.chmod(config.socket_path, 0o600)
        listener.listen(1)
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            authenticator = UnixSocketUbuntuLocalAuthenticator(
                connection=connection,
                socket_path=config.socket_path,
                connection_id=config.connection_id,
            )
            worker = UbuntuWorkerService(
                worker_id=config.worker_id,
                expected_peer=UbuntuLocalPeerExpectation(
                    peer_uid=config.gateway_uid,
                    socket_owner_uid=os.geteuid(),
                    socket_path=config.socket_path,
                ),
                authenticator=authenticator,
                readiness=lambda: UbuntuWorkerReadiness.READY,
                process_scope=process_scope,
            )
            try:
                serve_ubuntu_worker_connection(connection, worker, stop=stop)
            except (ActionDispatcherError, ConnectionError, OSError):
                connection.close()
    finally:
        listener.close()
        try:
            existing = path.lstat()
            if path.is_socket() and existing.st_uid == os.geteuid():
                path.unlink()
        except FileNotFoundError:
            pass


def run_windows_worker_loop(
    config: WindowsWorkerRuntimeConfig,
    stop: Event,
    *,
    client: Callable[..., None] = run_windows_worker_client,
) -> None:
    executor = SubprocessWindowsJobObjectExecutor(process_limit=config.process_limit)
    while not stop.is_set():
        try:
            client(config=config.mtls, application_hello=config.hello, executor=executor, stop=stop)
        except (ActionDispatcherError, OSError):
            pass
        if not stop.is_set():
            stop.wait(config.reconnect_seconds)


def _install_signal_handlers(stop: Event) -> None:
    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def run_windows_service(config: WindowsWorkerRuntimeConfig) -> None:
    if os.name != "nt":
        raise RuntimeError("Windows service hosting requires Windows")
    from ctypes import wintypes

    service_name = "JarvisWindowsWorker"
    stop = Event()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class ServiceStatus(ctypes.Structure):
        _fields_ = [
            ("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
            ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
            ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
            ("dwWaitHint", wintypes.DWORD),
        ]

    handler_type = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
    main_type = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
    status_handle = wintypes.HANDLE()

    advapi32.RegisterServiceCtrlHandlerExW.argtypes = [
        wintypes.LPCWSTR,
        handler_type,
        wintypes.LPVOID,
    ]
    advapi32.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
    advapi32.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(ServiceStatus)]
    advapi32.SetServiceStatus.restype = wintypes.BOOL

    def set_status(state: int, accepted: int = 0, exit_code: int = 0) -> None:
        status = ServiceStatus(0x10, state, accepted, exit_code, 0, 0, 30000 if state in {2, 3} else 0)
        if not advapi32.SetServiceStatus(status_handle, ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())

    @handler_type
    def handler(control: int, _event: int, _data: object, _context: object) -> int:
        if control in {1, 5}:
            stop.set()
            set_status(3)
        return 0

    @main_type
    def service_main(_argc: int, _argv: object) -> None:
        nonlocal status_handle
        status_handle = advapi32.RegisterServiceCtrlHandlerExW(service_name, handler, None)
        if not status_handle:
            return
        set_status(2)
        set_status(4, accepted=0x5)
        exit_code = 0
        try:
            run_windows_worker_loop(config, stop)
        except Exception:  # noqa: BLE001 - translate service failure to SCM status
            exit_code = 1066
        set_status(1, exit_code=exit_code)

    class ServiceTableEntry(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", ctypes.c_void_p)]

    advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(ServiceTableEntry)]
    advapi32.StartServiceCtrlDispatcherW.restype = wintypes.BOOL

    table = (ServiceTableEntry * 2)(
        ServiceTableEntry(service_name, ctypes.cast(service_main, ctypes.c_void_p)),
        ServiceTableEntry(None, None),
    )
    if not advapi32.StartServiceCtrlDispatcherW(table):
        raise ctypes.WinError(ctypes.get_last_error())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("ubuntu", "windows-console", "windows-service"):
        child = subparsers.add_parser(command)
        child.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    if args.command == "ubuntu":
        stop = Event()
        _install_signal_handlers(stop)
        run_ubuntu_worker(load_ubuntu_config(args.config), stop)
    else:
        config = load_windows_config(args.config)
        if args.command == "windows-service":
            run_windows_service(config)
        else:
            stop = Event()
            _install_signal_handlers(stop)
            run_windows_worker_loop(config, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
