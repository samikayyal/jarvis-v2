"""Run and verify the fixed Ticket 33 controlled endurance workload."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Literal, Protocol

SAMPLE_SECONDS = 5
SAMPLE_JITTER_SECONDS = 1.5
RUN_SECONDS = 2 * 60 * 60
REAL_RUN_SECONDS = 60 * 60
SETTLING_SECONDS = 10 * 60
MINIMUM_FREE_BYTES = 2 * 1024**3 + 16 * 1024**2
MAX_SWAP_GROWTH_BYTES = 256 * 1024**2

WORKLOADS: tuple[tuple[str, int, str], ...] = (
    (
        "bounded_read",
        60,
        (
            "tests/test_ticket17_google_reads.py::"
            "test_gmail_read_is_fixed_to_approved_scope_and_returns_a_bounded_result"
        ),
    ),
    (
        "multi_turn_read",
        20,
        (
            "tests/test_ticket14_agents_orchestration.py::"
            "test_agents_adapter_executes_one_closed_bounded_read_and_returns_milestone_and_final"
        ),
    ),
    (
        "approval",
        8,
        (
            "tests/test_ticket07_pending_actions.py::"
            "test_exact_approval_dispatches_the_frozen_payload_only_once"
        ),
    ),
    (
        "rejection",
        8,
        (
            "tests/test_ticket07_pending_actions.py::"
            "test_rejection_and_altered_approval_never_dispatch"
        ),
    ),
    (
        "terminal",
        4,
        (
            "tests/test_ticket11_worker_gateway.py::"
            "test_worker_gateway_forwards_only_bounded_non_interactive_execution"
        ),
    ),
    (
        "terminal_cancellation",
        4,
        (
            "tests/test_ticket11_worker_gateway.py::"
            "test_cancel_reconciles_the_running_selected_worker"
        ),
    ),
    (
        "terminal_output_cap",
        4,
        (
            "tests/test_ticket25_ubuntu_worker.py::"
            "test_ubuntu_worker_bounds_each_terminal_output_independently"
        ),
    ),
    (
        "timeout",
        3,
        (
            "tests/test_ticket11_worker_gateway.py::"
            "test_worker_gateway_enforces_deadline_then_cancels_the_process_scope"
        ),
    ),
    (
        "unavailability",
        3,
        (
            "tests/test_ticket11_worker_gateway.py::"
            "test_worker_gateway_rejects_mismatched_authenticated_identity_without_failover"
        ),
    ),
    (
        "ambiguous_outcome",
        3,
        (
            "tests/test_ticket07_pending_actions.py::"
            "test_ambiguous_dispatch_failure_is_recorded_as_unknown_without_a_retry"
        ),
    ),
    (
        "trace_capacity",
        3,
        (
            "tests/test_ticket04_diagnostic_traces.py::"
            "test_trace_capacity_rejects_before_connector_and_preserves_prior_trace"
        ),
    ),
)


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


def workload_plan() -> tuple[tuple[str, str], ...]:
    plan: list[tuple[str, str]] = []
    for kind, count, nodeid in WORKLOADS:
        plan.extend((kind, nodeid) for _ in range(count))
    return tuple(plan)


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


@dataclass(frozen=True, slots=True)
class EnduranceConfig:
    """Validated inputs for one controlled or supervised endurance run."""

    source_root: Path
    python: Path
    evidence: Path
    trace_root: Path
    temporary_root: Path
    run_seconds: int = RUN_SECONDS
    settling_seconds: int = SETTLING_SECONDS
    sample_seconds: int = SAMPLE_SECONDS
    smoke: bool = False
    external_workload: bool = False

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> EnduranceConfig:
        return cls(
            source_root=args.source_root,
            python=args.python,
            evidence=args.evidence,
            trace_root=args.trace_root,
            temporary_root=args.temporary_root,
            run_seconds=args.run_seconds,
            settling_seconds=args.settling_seconds,
            sample_seconds=args.sample_seconds,
            smoke=args.smoke,
            external_workload=args.external_workload,
        )


@dataclass(frozen=True, slots=True)
class EnduranceDependencies:
    """Local adapters used by the endurance coordinator.

    Keeping these dependencies in this module makes the acceptance harness
    independently controllable without widening the application's ports.
    """

    timing: Timing
    execute_workload: Callable[..., int]
    measure_host: Callable[..., ResourceMeasurement]
    prepare_evidence: Callable[[], None]
    write_evidence: Callable[[dict[str, object]], None]
    validate_samples: Callable[..., tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class EnduranceOutcome:
    """Typed result that also retains the stable JSONL summary contract."""

    mode: Literal["controlled", "external"]
    requests_planned: int
    requests_completed: int
    samples: int
    failures: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return int(bool(self.failures))

    def summary_record(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "requests_planned": self.requests_planned,
            "requests_completed": self.requests_completed,
            "samples": self.samples,
            "failures": list(self.failures),
        }


def production_dependencies(config: EnduranceConfig) -> EnduranceDependencies:
    """Compose the real system adapters used by the command-line entrypoint."""

    evidence = JsonlEvidenceWriter(config.evidence.resolve())
    return EnduranceDependencies(
        timing=SystemClock(),
        execute_workload=SubprocessWorkloadExecutor().execute,
        measure_host=SystemHostMeasurer().measure,
        prepare_evidence=evidence.prepare,
        write_evidence=evidence.write,
        validate_samples=validate_samples,
    )


class EnduranceRunner:
    """Coordinate scheduling, workload execution, evidence, and validation."""

    def __init__(self, dependencies: EnduranceDependencies) -> None:
        self._dependencies = dependencies

    def execute(self, config: EnduranceConfig) -> EnduranceOutcome:
        required_run_seconds = (
            REAL_RUN_SECONDS if config.external_workload else RUN_SECONDS
        )
        if not config.smoke and (
            config.run_seconds != required_run_seconds
            or config.settling_seconds != SETTLING_SECONDS
            or config.sample_seconds != SAMPLE_SECONDS
        ):
            raise ValueError(
                f"acceptance timing must remain {required_run_seconds}s + 600s "
                "with 5s samples"
            )

        plan = () if config.external_workload else workload_plan()
        dependencies = self._dependencies
        dependencies.prepare_evidence()
        samples: list[Sample] = []
        failures: list[str] = []
        requests_completed = 0
        started = dependencies.timing.monotonic()

        def sample_loop(stop: Sampling) -> None:
            next_sample = started
            while not stop.is_set():
                phase = (
                    "workload"
                    if dependencies.timing.monotonic() - started < config.run_seconds
                    else "settling"
                )
                try:
                    sample = collect_sample(
                        phase=phase,
                        started=started,
                        trace_root=config.trace_root,
                        temporary_root=config.temporary_root,
                        clock=dependencies.timing,
                        host_measurer=dependencies.measure_host,
                    )
                    samples.append(sample)
                    dependencies.write_evidence({"sample": asdict(sample)})
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    failures.append(f"sampling failed: {type(exc).__name__}")
                    stop.set()
                    return
                next_sample += config.sample_seconds
                stop.wait(max(0.0, next_sample - dependencies.timing.monotonic()))

        sampler = dependencies.timing.start(sample_loop)
        interval = config.run_seconds / len(plan) if plan else 0
        for index, (kind, nodeid) in enumerate(plan, start=1):
            due = started + (index - 1) * interval
            if not config.smoke:
                dependencies.timing.sleep(
                    max(0.0, due - dependencies.timing.monotonic())
                )
            exit_code = dependencies.execute_workload(
                python=config.python, source_root=config.source_root, nodeid=nodeid
            )
            dependencies.write_evidence(
                {
                    "request": index,
                    "kind": kind,
                    "nodeid": nodeid,
                    "exit_code": exit_code,
                }
            )
            if exit_code != 0:
                failures.append(f"controlled request {index} failed")
                break
            requests_completed += 1
            if config.smoke:
                break
        if not failures:
            dependencies.timing.sleep(
                max(
                    0.0,
                    started + config.run_seconds - dependencies.timing.monotonic(),
                )
            )
            dependencies.timing.sleep(config.settling_seconds)
        sampler.set()
        sampler.join(timeout=config.sample_seconds + 5)
        failures.extend(
            dependencies.validate_samples(
                samples,
                run_seconds=config.run_seconds,
                settling_seconds=config.settling_seconds,
                sample_seconds=config.sample_seconds,
            )
        )
        outcome = EnduranceOutcome(
            mode="external" if config.external_workload else "controlled",
            requests_planned=len(plan),
            requests_completed=requests_completed,
            samples=len(samples),
            failures=tuple(failures),
        )
        dependencies.write_evidence({"summary": outcome.summary_record()})
        print(json.dumps(outcome.summary_record(), sort_keys=True))
        return outcome


def run_endurance(
    config: EnduranceConfig,
    *,
    dependencies: EnduranceDependencies | None = None,
) -> EnduranceOutcome:
    """Run Ticket 33 through typed local adapters and return its outcome."""

    return EnduranceRunner(
        production_dependencies(config) if dependencies is None else dependencies
    ).execute(config)


def run(
    args: argparse.Namespace,
    *,
    dependencies: EnduranceDependencies | None = None,
) -> int:
    """CLI compatibility wrapper returning the historical process exit code."""

    return run_endurance(
        EnduranceConfig.from_namespace(args), dependencies=dependencies
    ).exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--trace-root", type=Path, required=True)
    parser.add_argument("--temporary-root", type=Path, required=True)
    parser.add_argument("--run-seconds", type=int, default=RUN_SECONDS)
    parser.add_argument("--settling-seconds", type=int, default=SETTLING_SECONDS)
    parser.add_argument("--sample-seconds", type=int, default=SAMPLE_SECONDS)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--external-workload", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
