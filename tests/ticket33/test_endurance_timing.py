from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from jarvis_control_plane import ticket33_endurance


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.value, UTC)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds

    def start(
        self, target: Callable[[ticket33_endurance.Sampling], None]
    ) -> ImmediateSampling:
        del target
        return ImmediateSampling()


class ImmediateSampling:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.stopped = True

    def wait(self, timeout: float) -> bool:
        del timeout
        return self.stopped

    def join(self, *, timeout: float) -> None:
        del timeout


class SuccessfulWorkload:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path, str]] = []

    def execute(self, *, python: Path, source_root: Path, nodeid: str) -> int:
        self.calls.append((python, source_root, nodeid))
        return 0


class ForbiddenWorkload:
    def execute(self, *, python: Path, source_root: Path, nodeid: str) -> int:
        del python, source_root, nodeid
        raise AssertionError("external workload must not run pytest")


class NoopHostMeasurer:
    def measure(
        self, *, trace_root: Path, temporary_root: Path
    ) -> ticket33_endurance.ResourceMeasurement:
        del trace_root, temporary_root
        return ticket33_endurance.ResourceMeasurement(
            available_memory_bytes=0,
            used_swap_bytes=0,
            free_disk_bytes=0,
            trace_bytes=0,
            trace_records=0,
            trace_payload_bytes=0,
            temporary_bytes=0,
            jarvis_cpu_percent=0.0,
            jarvis_memory_bytes=0,
            jarvis_pids=0,
        )


class MemoryEvidence:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.prepared = False

    def prepare(self) -> None:
        self.prepared = True

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)


def _dependencies(
    clock: FakeClock,
    workload: SuccessfulWorkload | ForbiddenWorkload,
    evidence: MemoryEvidence,
) -> ticket33_endurance.EnduranceDependencies:
    return ticket33_endurance.EnduranceDependencies(
        timing=clock,
        execute_workload=workload.execute,
        measure_host=NoopHostMeasurer().measure,
        prepare_evidence=evidence.prepare,
        write_evidence=evidence.write,
        validate_samples=lambda samples, **kwargs: (),
    )


def test_smoke_holds_the_complete_workload_and_settling_windows(
    tmp_path: Path, capsys
) -> None:
    clock = FakeClock()
    workload = SuccessfulWorkload()
    evidence = MemoryEvidence()
    args = argparse.Namespace(
        smoke=True,
        run_seconds=10,
        settling_seconds=5,
        sample_seconds=5,
        evidence=tmp_path / "evidence.jsonl",
        source_root=tmp_path,
        python=Path("python"),
        trace_root=tmp_path,
        temporary_root=tmp_path,
        external_workload=False,
    )

    assert (
        ticket33_endurance.run(
            args, dependencies=_dependencies(clock, workload, evidence)
        )
        == 0
    )
    assert evidence.prepared
    assert clock.sleeps == [10.0, 5]
    assert len(workload.calls) == 1
    assert evidence.records[-1] == {
        "summary": {
            "failures": [],
            "mode": "controlled",
            "requests_completed": 1,
            "requests_planned": 120,
            "samples": 0,
        }
    }
    captured = capsys.readouterr()
    assert json.loads(captured.out) == evidence.records[-1]["summary"]


def test_external_workload_samples_without_running_a_controlled_request(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    evidence = MemoryEvidence()
    args = argparse.Namespace(
        smoke=True,
        external_workload=True,
        run_seconds=10,
        settling_seconds=5,
        sample_seconds=5,
        evidence=tmp_path / "external.jsonl",
        source_root=tmp_path,
        python=Path("python"),
        trace_root=tmp_path,
        temporary_root=tmp_path,
    )

    assert (
        ticket33_endurance.run(
            args,
            dependencies=_dependencies(clock, ForbiddenWorkload(), evidence),
        )
        == 0
    )
    assert evidence.prepared
    assert clock.sleeps == [10.0, 5]
    assert evidence.records[-1] == {
        "summary": {
            "failures": [],
            "mode": "external",
            "requests_completed": 0,
            "requests_planned": 0,
            "samples": 0,
        }
    }


def test_typed_outcome_preserves_jsonl_summary_shape(tmp_path: Path) -> None:
    clock = FakeClock()
    evidence = MemoryEvidence()
    config = ticket33_endurance.EnduranceConfig(
        source_root=tmp_path,
        python=Path("python"),
        evidence=tmp_path / "evidence.jsonl",
        trace_root=tmp_path,
        temporary_root=tmp_path,
        smoke=True,
    )

    outcome = ticket33_endurance.run_endurance(
        config,
        dependencies=_dependencies(clock, SuccessfulWorkload(), evidence),
    )

    assert outcome.exit_code == 0
    assert outcome.failures == ()
    assert (
        json.loads(json.dumps({"summary": outcome.summary_record()}))
        == evidence.records[-1]
    )
