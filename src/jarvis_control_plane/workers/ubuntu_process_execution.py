"""Process launch, bounded output, and completion evidence for Ubuntu."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
from threading import Event, RLock, Thread
from time import monotonic
from typing import BinaryIO

from ..ports import ActionDispatcherError
from .contracts import (
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerInvocation,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    WorkerProgressSink,
)
from .ubuntu_process_models import _RunningSystemdScope, _StartingSystemdScope
from .ubuntu_worker_runner import COMPOUND_RESULT_MARKER

_COMPOUND_METADATA_LIMIT_BYTES = 4096


class _UbuntuProcessExecutionMixin:
    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        try:
            if sys.platform != "linux":
                raise ActionDispatcherError(
                    "native Ubuntu process scope requires Linux"
                )
            command = self.command_for(invocation)
        except ActionDispatcherError:
            with self._lock:
                self._reserved_action_ids.discard(invocation.action_id)
            raise
        with self._lock:
            if invocation.action_id in self._cancelled_action_ids:
                return WorkerExecutionResult(
                    status=WorkerExecutionStatus.CANCELLED,
                    process_tree_stopped=True,
                )
            if invocation.action_id not in self._reserved_action_ids:
                raise ActionDispatcherError("Ubuntu process scope was not reserved")
            self._reserved_action_ids.remove(invocation.action_id)
            self._active_action_ids.add(invocation.action_id)
            starting = _StartingSystemdScope(cancel_requested=Event(), resolved=Event())
            self._starting[invocation.action_id] = starting
        if starting.cancel_requested.is_set():
            with self._lock:
                self._starting.pop(invocation.action_id, None)
                self._active_action_ids.discard(invocation.action_id)
                self._cancelled_action_ids.add(invocation.action_id)
                starting.resolved.set()
            return WorkerExecutionResult(
                status=WorkerExecutionStatus.CANCELLED,
                process_tree_stopped=True,
            )
        unit_name = self._unit_name(invocation.action_id)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            with self._lock:
                self._active_action_ids.discard(invocation.action_id)
                self._starting.pop(invocation.action_id, None)
                if starting.cancel_requested.is_set():
                    self._cancelled_action_ids.add(invocation.action_id)
                starting.resolved.set()
            raise ActionDispatcherError(
                "native Ubuntu process scope could not start"
            ) from exc
        running = _RunningSystemdScope(
            unit_name=unit_name,
            process=process,
            cancel_requested=starting.cancel_requested,
            termination_lock=RLock(),
            unit_observed=Event(),
        )
        with self._lock:
            self._running[invocation.action_id] = running
            self._starting.pop(invocation.action_id, None)
            starting.resolved.set()
        if running.cancel_requested.is_set():
            stopped = self._stop_scope(running, invocation.cancellation_grace_seconds)
            process.stdout.close()
            process.stderr.close()
            if stopped:
                with self._lock:
                    if self._running.get(invocation.action_id) is running:
                        del self._running[invocation.action_id]
                    self._active_action_ids.discard(invocation.action_id)
                    self._stopped_action_ids.add(invocation.action_id)
            return WorkerExecutionResult(
                status=(
                    WorkerExecutionStatus.CANCELLED
                    if stopped
                    else WorkerExecutionStatus.UNKNOWN
                ),
                process_tree_stopped=stopped,
            )
        release_scope = False
        try:
            try:
                result = self._observe(running, invocation, progress)
            except BaseException:
                release_scope = self._stop_scope(
                    running, invocation.cancellation_grace_seconds
                )
                process.stdout.close()
                process.stderr.close()
                raise
            release_scope = result.process_tree_stopped
            return result
        finally:
            with self._lock:
                if self._running.get(invocation.action_id) is running and release_scope:
                    del self._running[invocation.action_id]
                    self._active_action_ids.discard(invocation.action_id)

    def _observe(
        self,
        running: _RunningSystemdScope,
        invocation: WorkerInvocation,
        progress: WorkerProgressSink,
    ) -> WorkerExecutionResult:
        # A small bounded queue applies backpressure to the pipe readers.  Each
        # reader also discards bytes beyond its own stream cap before enqueueing,
        # so fast child output can never accumulate unbounded Python memory.
        output: queue.Queue[tuple[WorkerOutputStream, bytes | None]] = queue.Queue(
            maxsize=8
        )

        def read_stream(
            stream: BinaryIO,
            tag: WorkerOutputStream,
            byte_limit: int,
            tail_limit: int = 0,
        ) -> None:
            read = getattr(stream, "read1", stream.read)
            remaining = byte_limit - tail_limit
            tail = bytearray()
            truncation_reported = False
            try:
                while chunk := read(16 * 1024):
                    accepted = chunk[:remaining]
                    if accepted:
                        output.put((tag, accepted))
                        remaining -= len(accepted)
                    excess = chunk[len(accepted) :]
                    if excess:
                        if tail_limit:
                            tail.extend(excess)
                            del tail[:-tail_limit]
                        if not truncation_reported:
                            output.put((tag, b""))
                            truncation_reported = True
            except (OSError, ValueError):
                # Deadline cleanup closes the pipes to release readers even if
                # a descendant inherited the wrapper's file descriptors.
                pass
            finally:
                try:
                    if tail:
                        output.put((tag, bytes(tail)), timeout=1)
                    output.put((tag, None), timeout=1)
                except queue.Full:
                    pass

        assert running.process.stdout is not None
        assert running.process.stderr is not None
        compound = len(invocation.action.components) > 1
        stderr_capture_limit = invocation.stderr_limit_bytes + (
            _COMPOUND_METADATA_LIMIT_BYTES if compound else 0
        )
        readers = (
            Thread(
                target=read_stream,
                args=(
                    running.process.stdout,
                    WorkerOutputStream.STDOUT,
                    invocation.stdout_limit_bytes,
                ),
                daemon=True,
            ),
            Thread(
                target=read_stream,
                args=(
                    running.process.stderr,
                    WorkerOutputStream.STDERR,
                    stderr_capture_limit,
                    _COMPOUND_METADATA_LIMIT_BYTES if compound else 0,
                ),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        sequence = 2
        emitted = 0
        if invocation.progress_event_limit > 1:
            progress(
                WorkerProgressEvent(
                    sequence=sequence,
                    kind=WorkerProgressKind.MILESTONE,
                    text="native-scope-started",
                )
            )
            sequence += 1
            emitted += 1
        buffers = {
            WorkerOutputStream.STDOUT: bytearray(),
            WorkerOutputStream.STDERR: bytearray(),
        }
        limits = {
            WorkerOutputStream.STDOUT: invocation.stdout_limit_bytes,
            WorkerOutputStream.STDERR: stderr_capture_limit,
        }
        truncated = {stream: False for stream in buffers}
        ended: set[WorkerOutputStream] = set()
        deadline = monotonic() + invocation.deadline_seconds
        timed_out = False
        cleanup_failed = False
        deadline_cleanup_stopped: bool | None = None
        next_unit_probe = monotonic()

        def record_chunk(stream: WorkerOutputStream, chunk: bytes | None) -> None:
            nonlocal emitted, sequence
            if chunk is None:
                ended.add(stream)
                return
            available = max(limits[stream] - len(buffers[stream]), 0)
            accepted = chunk[:available]
            buffers[stream].extend(accepted)
            chunk_truncated = not chunk or len(accepted) != len(chunk)
            truncated[stream] = truncated[stream] or chunk_truncated
            if (
                accepted
                and not (compound and stream is WorkerOutputStream.STDERR)
                and emitted < invocation.progress_event_limit - 1
            ):
                progress(
                    WorkerProgressEvent(
                        sequence=sequence,
                        kind=WorkerProgressKind.OUTPUT,
                        stream=stream,
                        text=accepted.decode(errors="replace"),
                        truncated=chunk_truncated,
                    )
                )
                sequence += 1
                emitted += 1

        while len(ended) < 2 or running.process.poll() is None:
            now = monotonic()
            if now >= deadline:
                timed_out = True
                deadline_cleanup_stopped = self._stop_scope(
                    running, invocation.cancellation_grace_seconds
                )
                cleanup_failed = not deadline_cleanup_stopped
                break
            if not running.unit_observed.is_set() and now >= next_unit_probe:
                self._unit_is_stopped(
                    running,
                    timeout_seconds=min(0.25, max(deadline - now, 0.001)),
                    wrapper_completed=False,
                )
                next_unit_probe = monotonic() + 0.25
            try:
                stream, chunk = output.get(timeout=0.05)
            except queue.Empty:
                continue
            record_chunk(stream, chunk)
        if timed_out:
            running.process.stdout.close()
            running.process.stderr.close()
        while True:
            try:
                stream, chunk = output.get_nowait()
            except queue.Empty:
                break
            record_chunk(stream, chunk)
        for reader in readers:
            reader.join(timeout=1)
        while True:
            try:
                stream, chunk = output.get_nowait()
            except queue.Empty:
                break
            record_chunk(stream, chunk)
        return_code = running.process.poll()
        process_tree_stopped = (
            deadline_cleanup_stopped
            if deadline_cleanup_stopped is not None
            else (
                not cleanup_failed
                and return_code is not None
                and self._unit_is_stopped(
                    running, timeout_seconds=2, wrapper_completed=True
                )
            )
        )
        status = (
            WorkerExecutionStatus.UNKNOWN
            if cleanup_failed
            else (
                WorkerExecutionStatus.TIMED_OUT
                if timed_out
                else (
                    WorkerExecutionStatus.CANCELLED
                    if running.cancel_requested.is_set()
                    else (
                        WorkerExecutionStatus.COMPLETED
                        if return_code == 0
                        else WorkerExecutionStatus.FAILED
                    )
                )
            )
        )
        started_components = (0,)
        completed_components = (0,) if status is WorkerExecutionStatus.COMPLETED else ()
        raw_stderr = bytes(buffers[WorkerOutputStream.STDERR])
        if compound:
            compound_result = _extract_compound_result(raw_stderr)
            if compound_result is None:
                if status in {
                    WorkerExecutionStatus.COMPLETED,
                    WorkerExecutionStatus.FAILED,
                }:
                    status = WorkerExecutionStatus.UNKNOWN
                raw_stderr = raw_stderr[: invocation.stderr_limit_bytes]
                started_components = ()
                completed_components = ()
            else:
                raw_stderr, started_components, completed_components = compound_result
            if raw_stderr and emitted < invocation.progress_event_limit - 1:
                progress(
                    WorkerProgressEvent(
                        sequence=sequence,
                        kind=WorkerProgressKind.OUTPUT,
                        stream=WorkerOutputStream.STDERR,
                        text=_render_captured(
                            raw_stderr,
                            truncated[WorkerOutputStream.STDERR],
                            invocation.stderr_limit_bytes,
                        ),
                        truncated=truncated[WorkerOutputStream.STDERR],
                    )
                )
        stdout = _render_captured(
            buffers[WorkerOutputStream.STDOUT],
            truncated[WorkerOutputStream.STDOUT],
            invocation.stdout_limit_bytes,
        )
        stderr = _render_captured(
            raw_stderr,
            truncated[WorkerOutputStream.STDERR],
            invocation.stderr_limit_bytes,
        )
        return WorkerExecutionResult(
            status=status,
            started_components=started_components,
            completed_components=completed_components,
            process_tree_stopped=process_tree_stopped,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=truncated[WorkerOutputStream.STDOUT],
            stderr_truncated=truncated[WorkerOutputStream.STDERR],
        )


def _extract_compound_result(
    captured: bytes,
) -> tuple[bytes, tuple[int, ...], tuple[int, ...]] | None:
    user_output, marker, suffix = captured.rpartition(COMPOUND_RESULT_MARKER)
    if not marker:
        return None
    metadata, separator, trailing = suffix.partition(b"\n")
    if not separator or trailing:
        return None
    try:
        value = json.loads(metadata)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"started", "completed"}:
        return None
    started = value["started"]
    completed = value["completed"]
    if not isinstance(started, list) or not isinstance(completed, list):
        return None
    if any(
        isinstance(index, bool) or not isinstance(index, int) or index < 0
        for index in (*started, *completed)
    ):
        return None
    started_tuple = tuple(started)
    completed_tuple = tuple(completed)
    if (
        tuple(sorted(set(started_tuple))) != started_tuple
        or tuple(sorted(set(completed_tuple))) != completed_tuple
        or not set(completed_tuple) <= set(started_tuple)
    ):
        return None
    return user_output, started_tuple, completed_tuple


def _render_captured(value: bytes | bytearray, truncated: bool, limit: int) -> str:
    text = bytes(value).decode(errors="replace")
    return _truncate_utf8(f"{text}\n[output truncated]", limit) if truncated else text


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    marker = "\n[output truncated]"
    room = max(limit - len(marker.encode()), 0)
    prefix = encoded[:room]
    while prefix:
        try:
            return prefix.decode() + marker
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return marker.encode()[:limit].decode(errors="ignore")
