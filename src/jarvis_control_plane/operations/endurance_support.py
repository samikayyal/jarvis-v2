"""Sampling, resource measurement, and evidence support for endurance runs."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Protocol

SAMPLE_JITTER_SECONDS = 1.5
SAMPLE_SECONDS = 5
RUN_SECONDS = 2 * 60 * 60
SETTLING_SECONDS = 10 * 60
MINIMUM_FREE_BYTES = 2 * 1024**3 + 16 * 1024**2
MAX_SWAP_GROWTH_BYTES = 256 * 1024**2


class Sampling(Protocol):
    """Stop and join operations for one sampling loop."""

    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float) -> bool: ...

    def join(self, *, timeout: float) -> None: ...


class Timing(Protocol):
    """Clock plus local control of the sampling loop."""

    def monotonic(self) -> float: ...

    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...

    def start(self, target: Callable[[Sampling], None]) -> Sampling: ...


@dataclass(frozen=True, slots=True)
class Sample:
    phase: str
    monotonic_seconds: float
    occurred_at: str
    available_memory_bytes: int
    used_swap_bytes: int
    free_disk_bytes: int
    trace_bytes: int
    trace_records: int
    trace_payload_bytes: int
    temporary_bytes: int
    jarvis_cpu_percent: float
    jarvis_memory_bytes: int
    jarvis_pids: int


@dataclass(frozen=True, slots=True)
class ResourceMeasurement:
    """Resource values shared by production and controlled measurements."""

    available_memory_bytes: int
    used_swap_bytes: int
    free_disk_bytes: int
    trace_bytes: int
    trace_records: int
    trace_payload_bytes: int
    temporary_bytes: int
    jarvis_cpu_percent: float
    jarvis_memory_bytes: int
    jarvis_pids: int


class HostMeasurer(Protocol):
    """Collect the resource fields needed for one sample."""

    def measure(
        self, *, trace_root: Path, temporary_root: Path
    ) -> ResourceMeasurement: ...


class _ThreadSampling:
    def __init__(self, target: Callable[[Sampling], None]) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=lambda: target(self._stop), daemon=True)
        self._thread.start()

    def is_set(self) -> bool:
        return self._stop.is_set()

    def set(self) -> None:
        self._stop.set()

    def wait(self, timeout: float) -> bool:
        return self._stop.wait(timeout)

    def join(self, *, timeout: float) -> None:
        self._thread.join(timeout=timeout)


class SystemClock:
    """Production clock and sampling scheduler."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def start(self, target: Callable[[Sampling], None]) -> Sampling:
        return _ThreadSampling(target)


def _tree_size(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _trace_facts(root: Path) -> tuple[int, int]:
    database = root / "traces.sqlite3"
    if not database.exists():
        return 0, 0
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(payload_bytes), 0) FROM diagnostic_traces"
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _memory() -> tuple[int, int]:
    fields: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        fields[key] = int(value.strip().split()[0]) * 1024
    return fields["MemAvailable"], fields["SwapTotal"] - fields["SwapFree"]


def _docker_stats(
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[float, int, int]:
    observed = runner(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.PIDs}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    cpu = 0.0
    memory = 0
    pids = 0
    for line in observed.stdout.splitlines():
        name, cpu_text, memory_text, pid_text = line.split("|", 3)
        if not name.startswith("jarvis-assistant-v1-"):
            continue
        cpu += float(cpu_text.removesuffix("%"))
        match = re.fullmatch(
            r"([0-9]+(?:\.[0-9]+)?)\s*(B|KiB|MiB|GiB)",
            memory_text.split("/", 1)[0].strip(),
        )
        if match is None:
            raise ValueError("Docker returned an invalid memory measurement")
        value, unit = match.groups()
        scale = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[unit]
        memory += int(float(value) * scale)
        pids += int(pid_text)
    return cpu, memory, pids


class SubprocessWorkloadExecutor:
    """Production adapter for one bounded pytest workload invocation."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._runner = subprocess.run if runner is None else runner

    def execute(self, *, python: Path, source_root: Path, nodeid: str) -> int:
        completed = self._runner(
            [str(python), "-m", "pytest", "-q", nodeid],
            cwd=source_root,
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode


class SystemHostMeasurer:
    """Production adapter for host, container, trace, and temporary usage."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._runner = subprocess.run if runner is None else runner

    def measure(self, *, trace_root: Path, temporary_root: Path) -> ResourceMeasurement:
        available, swap = _memory()
        cpu, memory, pids = _docker_stats(self._runner)
        trace_records, trace_payload_bytes = _trace_facts(trace_root)
        return ResourceMeasurement(
            available_memory_bytes=available,
            used_swap_bytes=swap,
            free_disk_bytes=shutil.disk_usage(trace_root).free,
            trace_bytes=_tree_size(trace_root),
            trace_records=trace_records,
            trace_payload_bytes=trace_payload_bytes,
            temporary_bytes=_tree_size(temporary_root),
            jarvis_cpu_percent=cpu,
            jarvis_memory_bytes=memory,
            jarvis_pids=pids,
        )


class JsonlEvidenceWriter:
    """Create a private evidence file and append its stable JSONL records."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def prepare(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(mode=0o600, exist_ok=False)

    def write(self, record: dict[str, object]) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def collect_sample(
    *,
    phase: str,
    started: float,
    trace_root: Path,
    temporary_root: Path,
    clock: Timing | None = None,
    host_measurer: HostMeasurer | Callable[..., ResourceMeasurement] | None = None,
) -> Sample:
    clock = SystemClock() if clock is None else clock
    selected_measurer = SystemHostMeasurer() if host_measurer is None else host_measurer
    measure = (
        selected_measurer if callable(selected_measurer) else selected_measurer.measure
    )
    measurement = measure(trace_root=trace_root, temporary_root=temporary_root)
    return Sample(
        phase=phase,
        monotonic_seconds=round(clock.monotonic() - started, 3),
        occurred_at=clock.now().isoformat(),
        available_memory_bytes=measurement.available_memory_bytes,
        used_swap_bytes=measurement.used_swap_bytes,
        free_disk_bytes=measurement.free_disk_bytes,
        trace_bytes=measurement.trace_bytes,
        trace_records=measurement.trace_records,
        trace_payload_bytes=measurement.trace_payload_bytes,
        temporary_bytes=measurement.temporary_bytes,
        jarvis_cpu_percent=measurement.jarvis_cpu_percent,
        jarvis_memory_bytes=measurement.jarvis_memory_bytes,
        jarvis_pids=measurement.jarvis_pids,
    )


def validate_samples(
    samples: Sequence[Sample],
    *,
    run_seconds: int = RUN_SECONDS,
    settling_seconds: int = SETTLING_SECONDS,
    sample_seconds: int = SAMPLE_SECONDS,
) -> tuple[str, ...]:
    failures: list[str] = []
    if not samples or {item.phase for item in samples} != {"workload", "settling"}:
        failures.append("both workload and settling samples are required")
        return tuple(failures)
    if (
        samples[0].monotonic_seconds > sample_seconds
        or samples[-1].monotonic_seconds
        < run_seconds + settling_seconds - sample_seconds
    ):
        failures.append("the complete workload and settling interval was not sampled")
    if any(
        later.monotonic_seconds - earlier.monotonic_seconds
        > sample_seconds + SAMPLE_JITTER_SECONDS
        for earlier, later in pairwise(samples)
    ):
        failures.append("resource samples were not collected every five seconds")
    if min(item.free_disk_bytes for item in samples) < MINIMUM_FREE_BYTES:
        failures.append("free disk crossed the protected floor")
    if max(item.jarvis_cpu_percent for item in samples) > 200.0:
        failures.append("Jarvis crossed the two-core CPU ceiling")
    if max(item.jarvis_memory_bytes for item in samples) > 1280 * 1024**2:
        failures.append("Jarvis crossed the aggregate memory ceiling")
    if max(item.jarvis_pids for item in samples) > 512:
        failures.append("Jarvis crossed the aggregate PID ceiling")
    if (
        max(item.used_swap_bytes for item in samples) - samples[0].used_swap_bytes
        > MAX_SWAP_GROWTH_BYTES
    ):
        failures.append("host swap grew by more than 256 MiB")
    trace_record_deltas = [
        later.trace_records - earlier.trace_records
        for earlier, later in pairwise(samples)
    ]
    if any(delta < 0 for delta in trace_record_deltas):
        failures.append("diagnostic traces were deleted during endurance")
    trace_payload_deltas = [
        later.trace_payload_bytes - earlier.trace_payload_bytes
        for earlier, later in pairwise(samples)
    ]
    if any(delta > 16 * 1024**2 for delta in trace_payload_deltas):
        failures.append("trace growth crossed the per-request reservation")
    settling = [item for item in samples if item.phase == "settling"]
    if settling[-1].used_swap_bytes > settling[0].used_swap_bytes:
        failures.append("swap continued to grow during settling")
    if settling[-1].temporary_bytes > samples[0].temporary_bytes:
        failures.append("temporary request data was not reclaimed")
    return tuple(failures)
