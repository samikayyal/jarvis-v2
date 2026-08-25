from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from jarvis_control_plane.ticket33_endurance import (
    MAX_SWAP_GROWTH_BYTES,
    MINIMUM_FREE_BYTES,
    Sample,
    validate_samples,
    workload_plan,
)


def _sample(*, phase: str, seconds: float, **changes: object) -> Sample:
    sample = Sample(
        phase=phase,
        monotonic_seconds=seconds,
        occurred_at="2026-08-25T00:00:00+00:00",
        available_memory_bytes=1024**3,
        used_swap_bytes=100,
        free_disk_bytes=MINIMUM_FREE_BYTES,
        trace_bytes=100,
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
        ),
        _sample(
            phase="settling",
            seconds=7800,
            used_swap_bytes=MAX_SWAP_GROWTH_BYTES + 102,
            temporary_bytes=101,
            trace_bytes=100,
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
