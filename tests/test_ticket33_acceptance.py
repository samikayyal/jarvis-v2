from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from jarvis_control_plane.ticket33_endurance import (
    MAX_SWAP_GROWTH_BYTES,
    MINIMUM_FREE_BYTES,
    EnduranceConfig,
    EnduranceDependencies,
    ResourceMeasurement,
    Sample,
    Sampling,
    run_endurance,
    validate_samples,
    workload_plan,
)


class _OneSample:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.stopped = True

    def wait(self, timeout: float) -> bool:
        del timeout
        self.stopped = True
        return True

    def join(self, *, timeout: float) -> None:
        del timeout


class _SynchronousTiming:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.value, UTC)

    def sleep(self, seconds: float) -> None:
        self.value += seconds

    def start(self, target: Callable[[Sampling], None]) -> _OneSample:
        sampler = _OneSample()
        target(sampler)
        return sampler


class _Evidence:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def prepare(self) -> None:
        return None

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)


def _config(tmp_path: Path) -> EnduranceConfig:
    return EnduranceConfig(
        source_root=tmp_path,
        python=Path("python"),
        evidence=tmp_path / "evidence.jsonl",
        trace_root=tmp_path,
        temporary_root=tmp_path,
        run_seconds=10,
        settling_seconds=5,
        sample_seconds=5,
        smoke=True,
    )


def _dependencies(
    timing: _SynchronousTiming,
    evidence: _Evidence,
    measure_host: Callable[..., ResourceMeasurement],
    execute_workload: Callable[..., int],
) -> EnduranceDependencies:
    return EnduranceDependencies(
        timing=timing,
        execute_workload=execute_workload,
        measure_host=measure_host,
        prepare_evidence=evidence.prepare,
        write_evidence=evidence.write,
        validate_samples=lambda samples, **kwargs: (),
    )


def test_runner_measures_and_writes_samples_through_injected_adapters(
    tmp_path: Path,
) -> None:
    evidence = _Evidence()
    measurement = ResourceMeasurement(
        available_memory_bytes=1024,
        used_swap_bytes=100,
        free_disk_bytes=MINIMUM_FREE_BYTES,
        trace_bytes=100,
        trace_records=100,
        trace_payload_bytes=100,
        temporary_bytes=100,
        jarvis_cpu_percent=100.0,
        jarvis_memory_bytes=1024,
        jarvis_pids=400,
    )
    measured: list[tuple[Path, Path]] = []
    executed: list[str] = []

    def measure_host(*, trace_root: Path, temporary_root: Path) -> ResourceMeasurement:
        measured.append((trace_root, temporary_root))
        return measurement

    def execute_workload(*, python: Path, source_root: Path, nodeid: str) -> int:
        del python, source_root
        executed.append(nodeid)
        return 0

    outcome = run_endurance(
        _config(tmp_path),
        dependencies=_dependencies(
            _SynchronousTiming(), evidence, measure_host, execute_workload
        ),
    )

    assert outcome.exit_code == 0
    assert measured == [(tmp_path, tmp_path)]
    assert len(executed) == 1
    sample_record = evidence.records[0]["sample"]
    assert isinstance(sample_record, dict)
    for field, value in asdict(measurement).items():
        assert sample_record[field] == value
    assert evidence.records[-1] == {"summary": outcome.summary_record()}


def test_runner_fails_closed_when_sampling_fails_and_records_outcome(
    tmp_path: Path,
) -> None:
    evidence = _Evidence()

    def measure_host(*, trace_root: Path, temporary_root: Path) -> ResourceMeasurement:
        del trace_root, temporary_root
        raise ValueError("host measurement unavailable")

    def execute_workload(*, python: Path, source_root: Path, nodeid: str) -> int:
        del python, source_root, nodeid
        return 0

    outcome = run_endurance(
        _config(tmp_path),
        dependencies=_dependencies(
            _SynchronousTiming(), evidence, measure_host, execute_workload
        ),
    )

    assert outcome.exit_code == 1
    assert outcome.failures == ("sampling failed: ValueError",)
    assert evidence.records[-1] == {"summary": outcome.summary_record()}


def _sample(*, phase: str, seconds: float, **changes: object) -> Sample:
    sample = Sample(
        phase=phase,
        monotonic_seconds=seconds,
        occurred_at="2026-08-25T00:00:00+00:00",
        available_memory_bytes=1024**3,
        used_swap_bytes=100,
        free_disk_bytes=MINIMUM_FREE_BYTES,
        trace_bytes=100,
        trace_records=100,
        trace_payload_bytes=100,
        temporary_bytes=100,
        jarvis_cpu_percent=100.0,
        jarvis_memory_bytes=1024**3,
        jarvis_pids=400,
    )
    return replace(sample, **changes)


def test_fixed_workload_has_the_exact_ticket33_mix() -> None:
    plan = workload_plan()

    assert len(plan) == 120
    counts: dict[str, int] = {}
    for kind, _nodeid in plan:
        counts[kind] = counts.get(kind, 0) + 1
    assert counts == {
        "bounded_read": 60,
        "multi_turn_read": 20,
        "approval": 8,
        "rejection": 8,
        "terminal": 4,
        "terminal_cancellation": 4,
        "terminal_output_cap": 4,
        "timeout": 3,
        "unavailability": 3,
        "ambiguous_outcome": 3,
        "trace_capacity": 3,
    }


def test_resource_validator_accepts_only_the_complete_envelope() -> None:
    samples = tuple(
        _sample(phase="workload" if seconds < 7200 else "settling", seconds=seconds)
        for seconds in range(0, 7801, 5)
    )

    assert validate_samples(samples) == ()
    assert validate_samples(samples[:1]) == (
        "both workload and settling samples are required",
    )


def test_resource_validator_reports_each_protected_boundary() -> None:
    samples = (
        _sample(phase="workload", seconds=0),
        _sample(
            phase="settling",
            seconds=7200,
            used_swap_bytes=MAX_SWAP_GROWTH_BYTES + 101,
            free_disk_bytes=MINIMUM_FREE_BYTES - 1,
            jarvis_cpu_percent=200.1,
            jarvis_memory_bytes=1280 * 1024**2 + 1,
            jarvis_pids=513,
            temporary_bytes=101,
            trace_bytes=16 * 1024**2 + 101,
            trace_payload_bytes=16 * 1024**2 + 101,
        ),
        _sample(
            phase="settling",
            seconds=7800,
            used_swap_bytes=MAX_SWAP_GROWTH_BYTES + 102,
            temporary_bytes=101,
            trace_bytes=100,
            trace_records=99,
            trace_payload_bytes=100,
        ),
    )

    assert set(validate_samples(samples, sample_seconds=7200)) == {
        "free disk crossed the protected floor",
        "Jarvis crossed the two-core CPU ceiling",
        "Jarvis crossed the aggregate memory ceiling",
        "Jarvis crossed the aggregate PID ceiling",
        "host swap grew by more than 256 MiB",
        "swap continued to grow during settling",
        "temporary request data was not reclaimed",
        "trace growth crossed the per-request reservation",
        "diagnostic traces were deleted during endurance",
    }


def test_resource_validator_ignores_sqlite_journal_file_shrink() -> None:
    samples = (
        _sample(phase="workload", seconds=0, trace_bytes=20_000),
        _sample(phase="settling", seconds=7200, trace_bytes=19_000),
        _sample(phase="settling", seconds=7800, trace_bytes=19_000),
    )

    assert validate_samples(samples, sample_seconds=7200) == ()


def test_resource_validator_rejects_missing_sample_intervals() -> None:
    samples = (
        _sample(phase="workload", seconds=0),
        _sample(phase="settling", seconds=7200),
        _sample(phase="settling", seconds=7790),
    )

    assert set(validate_samples(samples)) == {
        "the complete workload and settling interval was not sampled",
        "resource samples were not collected every five seconds",
    }


def test_resource_validator_allows_bounded_scheduler_jitter() -> None:
    samples = tuple(
        _sample(
            phase="workload" if seconds < 7200 else "settling",
            seconds=seconds + (1.082 if seconds == 3610 else 0),
        )
        for seconds in range(0, 7801, 5)
    )

    assert validate_samples(samples) == ()


def test_ticket33_runbook_locks_every_supervised_gate() -> None:
    runbook = Path("deployment/ticket33-acceptance-runbook.md").read_text(
        encoding="utf-8"
    )

    assert all(f"| {gate:02d} |" in runbook for gate in range(1, 13))
    normalized = runbook.lower()
    for required in (
        "two continuous hours",
        "60-minute real workload",
        "five-second samples",
        "ten-minute settling window",
        "low-disk and trace-capacity",
        "audit-service outage",
        "pre-change",
        "--force-failure",
        "openwa must not be stopped, recreated, re-paired",
        "systemctl reboot",
        "interrupted",
        "session permission revoked",
        "physical phone receipt",
    ):
        assert required in normalized


def test_endurance_wrapper_restores_only_the_existing_receiver() -> None:
    wrapper = Path("deployment/ticket33-endurance-wrapper.sh").read_text(
        encoding="utf-8"
    )

    assert "trap restore_admission EXIT" in wrapper
    assert '"${compose[@]}" stop inbound_receiver' in wrapper
    assert '"${compose[@]}" start inbound_receiver' in wrapper
    assert "--force-recreate" not in wrapper
    assert "--volumes" not in wrapper
    assert "openwa" not in wrapper.lower()
