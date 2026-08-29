"""Archive IPC server, supervised process entry point, and local launcher."""

from __future__ import annotations

import os
import signal
import stat
import tempfile
import uuid
from multiprocessing import Pipe, Process
from multiprocessing.connection import Listener
from pathlib import Path
from threading import Thread, current_thread, main_thread
from typing import Any, NoReturn

from ..ports import DeletedConversationArchiveError
from .sqlite_storage import (
    abort_archive_batch,
    abort_incomplete_archive_batches,
    append_archive_batch_chunk,
    archive_batch,
    archive_database_path,
    begin_archive_batch,
    commit_archive_batch,
    open_archive_connection,
)
from .sqlite_writer import (
    DEFAULT_ARCHIVE_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_ARCHIVE_RESPONSE_TIMEOUT_SECONDS,
    SQLiteDeletedConversationArchiveWriter,
)
from .wire import (
    DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
    ArchiveWireCodec,
    send_archive_response,
)


def terminate_archive_service(_signum: int, _frame: object) -> NoReturn:
    raise SystemExit


_terminate_archive_service = terminate_archive_service


def archive_ipc_family() -> str:
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


_archive_ipc_family = archive_ipc_family


def validate_archive_authkey(authkey: bytes) -> bytes:
    if not isinstance(authkey, bytes) or not authkey:
        raise ValueError("deleted archive IPC authkey must be non-empty bytes")
    return authkey


_validate_archive_authkey = validate_archive_authkey


def archive_endpoint_path(endpoint: str | Path) -> Path:
    if os.name == "nt":
        raise RuntimeError("Windows named-pipe endpoints do not have filesystem paths")
    return Path(endpoint).expanduser().resolve()


_archive_endpoint_path = archive_endpoint_path


def create_archive_listener(endpoint: str | Path, authkey: bytes) -> Listener:
    family = archive_ipc_family()
    if family == "AF_UNIX":
        endpoint_path = archive_endpoint_path(endpoint)
        if endpoint_path.exists():
            raise DeletedConversationArchiveError(
                "deleted archive IPC endpoint already exists"
            )
        endpoint_path.parent.mkdir(parents=True, exist_ok=True)
        listener = Listener(
            str(endpoint_path),
            family=family,
            authkey=validate_archive_authkey(authkey),
        )
        os.chmod(
            endpoint_path,
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP,
        )
        return listener
    return Listener(
        str(endpoint),
        family=family,
        authkey=validate_archive_authkey(authkey),
    )


_create_archive_listener = create_archive_listener


def remove_archive_endpoint(endpoint: str | Path) -> None:
    if os.name != "nt":
        archive_endpoint_path(endpoint).unlink(missing_ok=True)


_remove_archive_endpoint = remove_archive_endpoint


def _serve_archive_connection(
    connection: Any,
    database: str | Path,
    codec: ArchiveWireCodec | None = None,
) -> None:
    wire = codec or ArchiveWireCodec()
    archive_connection = None
    active_batch_ids: set[str] = set()
    try:
        archive_connection = open_archive_connection(database)
        send_archive_response(
            connection,
            ok=True,
            max_frame_bytes=wire.max_frame_bytes,
        )
        while True:
            try:
                request = wire.decode_request(
                    connection.recv_bytes(wire.max_frame_bytes)
                )
            except (EOFError, OSError):
                return
            except (TypeError, ValueError) as exc:
                try:
                    send_archive_response(
                        connection,
                        ok=False,
                        message=str(exc),
                        max_frame_bytes=wire.max_frame_bytes,
                    )
                except (BrokenPipeError, EOFError, OSError, ValueError):
                    return
                continue
            operation = request.get("operation")
            try:
                if operation == "archive":
                    records = request["messages"]
                    deletion_id = request["deletion_id"]
                    deleted_at = request["deleted_at"]
                    if (
                        not isinstance(records, tuple)
                        or not isinstance(deletion_id, str)
                        or not hasattr(deleted_at, "isoformat")
                    ):
                        raise DeletedConversationArchiveError(
                            "deleted archive service received an invalid archive request"
                        )
                    archive_batch(
                        archive_connection,
                        records,
                        deletion_id=deletion_id,
                        deleted_at=deleted_at,
                    )
                elif operation == "begin":
                    begin_archive_batch(
                        archive_connection,
                        deletion_id=request["deletion_id"],
                        deleted_at=request["deleted_at"],
                        expected_count=request["expected_count"],
                        expected_digest=request["expected_digest"],
                    )
                    active_batch_ids.add(request["deletion_id"])
                elif operation == "chunk":
                    append_archive_batch_chunk(
                        archive_connection,
                        request["messages"],
                        deletion_id=request["deletion_id"],
                        chunk_index=request["chunk_index"],
                    )
                elif operation == "commit":
                    commit_archive_batch(
                        archive_connection,
                        deletion_id=request["deletion_id"],
                    )
                    active_batch_ids.discard(request["deletion_id"])
                elif operation == "abort":
                    abort_archive_batch(
                        archive_connection,
                        deletion_id=request["deletion_id"],
                    )
                    active_batch_ids.discard(request["deletion_id"])
                else:
                    raise DeletedConversationArchiveError(
                        "deleted archive service received an unsupported operation"
                    )
                send_archive_response(
                    connection,
                    ok=True,
                    max_frame_bytes=wire.max_frame_bytes,
                )
            except Exception as exc:  # noqa: BLE001 - IPC reports typed failures
                try:
                    send_archive_response(
                        connection,
                        ok=False,
                        message=str(exc),
                        max_frame_bytes=wire.max_frame_bytes,
                    )
                except (BrokenPipeError, EOFError, OSError, ValueError):
                    return
    finally:
        if archive_connection is not None:
            abort_incomplete_archive_batches(archive_connection, active_batch_ids)
            archive_connection.close()
        try:
            connection.close()
        except OSError:
            pass


def serve_archive(
    database: str | Path,
    endpoint: str | Path,
    *,
    authkey: bytes,
    create_listener: Any = create_archive_listener,
    remove_endpoint: Any = remove_archive_endpoint,
    signal_module: Any = signal,
    terminate_handler: Any = terminate_archive_service,
    frame_limit: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
) -> None:
    """Serve the archive from a separately supervised administrative process."""

    listener = create_listener(endpoint, authkey)
    previous_sigterm = (
        signal_module.signal(signal_module.SIGTERM, terminate_handler)
        if current_thread() is main_thread()
        else None
    )
    try:
        while True:
            connection = listener.accept()
            Thread(
                target=_serve_archive_connection,
                args=(connection, database, ArchiveWireCodec(frame_limit)),
                daemon=True,
            ).start()
    finally:
        listener.close()
        remove_endpoint(endpoint)
        if previous_sigterm is not None:
            signal_module.signal(signal_module.SIGTERM, previous_sigterm)


def archive_service_process_main(
    startup_connection: Any,
    database: str,
    endpoint: str,
    authkey: bytes,
    frame_limit: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
) -> None:
    listener: Listener | None = None
    try:
        listener = create_archive_listener(endpoint, authkey)
        send_archive_response(
            startup_connection,
            ok=True,
            max_frame_bytes=frame_limit,
        )
        startup_connection.close()
        while True:
            connection = listener.accept()
            Thread(
                target=_serve_archive_connection,
                args=(connection, database, ArchiveWireCodec(frame_limit)),
                daemon=True,
            ).start()
    except Exception as exc:  # noqa: BLE001 - startup boundary reports typed errors
        try:
            send_archive_response(
                startup_connection,
                ok=False,
                message=str(exc),
                max_frame_bytes=frame_limit,
            )
        except (BrokenPipeError, EOFError, OSError, ValueError):
            pass
    finally:
        if listener is not None:
            listener.close()
            remove_archive_endpoint(endpoint)


_archive_service_process_main = archive_service_process_main


class SQLiteDeletedConversationArchiveService:
    """Test/development launcher for a separately addressed archive service."""

    def __init__(
        self,
        database: str | Path,
        *,
        endpoint: str | Path | None = None,
        authkey: bytes | None = None,
        writer_factory: Any = SQLiteDeletedConversationArchiveWriter,
        remove_endpoint: Any = remove_archive_endpoint,
        frame_limit: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
        response_timeout_seconds: float = DEFAULT_ARCHIVE_RESPONSE_TIMEOUT_SECONDS,
        close_timeout_seconds: float = DEFAULT_ARCHIVE_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        archive_database_path(database)
        self._owns_endpoint_directory = endpoint is None and os.name != "nt"
        if endpoint is None:
            if os.name == "nt":
                endpoint = rf"\\.\pipe\jarvis-deleted-{uuid.uuid4().hex}"
            else:
                directory = Path(tempfile.mkdtemp(prefix="jarvis-deleted-"))
                endpoint = directory / "writer.sock"
        self.endpoint = str(endpoint)
        self._endpoint_directory = (
            Path(self.endpoint).parent if self._owns_endpoint_directory else None
        )
        self._authkey = validate_archive_authkey(authkey or os.urandom(32))
        self._remove_endpoint = remove_endpoint
        self._frame_limit = frame_limit
        self._response_timeout_seconds = response_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        startup_parent, startup_child = Pipe(duplex=True)
        self._process = Process(
            target=archive_service_process_main,
            args=(
                startup_child,
                str(database),
                self.endpoint,
                self._authkey,
                self._frame_limit,
            ),
            daemon=True,
        )
        try:
            self._process.start()
        except Exception:
            startup_parent.close()
            startup_child.close()
            self._cleanup()
            raise
        finally:
            startup_child.close()
        try:
            if not startup_parent.poll(self._response_timeout_seconds):
                raise DeletedConversationArchiveError(
                    "deleted archive service did not start"
                )
            response = ArchiveWireCodec(self._frame_limit).decode_response(
                startup_parent.recv_bytes(self._frame_limit)
            )
        except DeletedConversationArchiveError:
            self.close()
            raise
        except (EOFError, OSError, TypeError, ValueError) as exc:
            self.close()
            raise DeletedConversationArchiveError(
                "deleted archive service did not start"
            ) from exc
        finally:
            startup_parent.close()
        if not response["ok"]:
            message = str(response.get("message", "deleted archive service failed"))
            self.close()
            raise DeletedConversationArchiveError(message[:200])
        try:
            self.writer = writer_factory(
                self.endpoint,
                authkey=self._authkey,
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        writer = getattr(self, "writer", None)
        if writer is not None:
            writer.close()
            self.writer = None
        process = getattr(self, "_process", None)
        if process is not None:
            process.join(self._close_timeout_seconds)
            if process.is_alive():
                process.terminate()
                process.join(self._close_timeout_seconds)
        self._cleanup()

    def _cleanup(self) -> None:
        if self._endpoint_directory is not None:
            self._remove_endpoint(self.endpoint)
            try:
                self._endpoint_directory.rmdir()
            except OSError:
                pass


def start_archive_service(
    database: str | Path,
    *,
    endpoint: str | Path | None = None,
    authkey: bytes | None = None,
    writer_factory: Any = SQLiteDeletedConversationArchiveWriter,
    remove_endpoint: Any = remove_archive_endpoint,
    frame_limit: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
    response_timeout_seconds: float = DEFAULT_ARCHIVE_RESPONSE_TIMEOUT_SECONDS,
    close_timeout_seconds: float = DEFAULT_ARCHIVE_CLOSE_TIMEOUT_SECONDS,
) -> SQLiteDeletedConversationArchiveService:
    return SQLiteDeletedConversationArchiveService(
        database,
        endpoint=endpoint,
        authkey=authkey,
        writer_factory=writer_factory,
        remove_endpoint=remove_endpoint,
        frame_limit=frame_limit,
        response_timeout_seconds=response_timeout_seconds,
        close_timeout_seconds=close_timeout_seconds,
    )


__all__ = [
    "SQLiteDeletedConversationArchiveService",
    "archive_ipc_family",
    "create_archive_listener",
    "remove_archive_endpoint",
    "serve_archive",
    "start_archive_service",
    "terminate_archive_service",
    "validate_archive_authkey",
]
