"""Run and verify the fixed Ticket 33 controlled endurance workload."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

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


@dataclass(frozen=True, slots=True)
class Sample:
    phase: str
    monotonic_seconds: float
    occurred_at: str
    available_memory_bytes: int
    used_swap_bytes: int
    free_disk_bytes: int
    trace_bytes: int
    temporary_bytes: int
    jarvis_cpu_percent: float
    jarvis_memory_bytes: int
    jarvis_pids: int


def workload_plan() -> tuple[tuple[str, str], ...]:
    plan: list[tuple[str, str]] = []
    for kind, count, nodeid in WORKLOADS:
        plan.extend((kind, nodeid) for _ in range(count))
    return tuple(plan)


def _tree_size(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


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


def collect_sample(
    *, phase: str, started: float, trace_root: Path, temporary_root: Path
) -> Sample:
    available, swap = _memory()
    cpu, memory, pids = _docker_stats(subprocess.run)
    return Sample(
        phase=phase,
        monotonic_seconds=round(time.monotonic() - started, 3),
        occurred_at=datetime.now(UTC).isoformat(),
        available_memory_bytes=available,
        used_swap_bytes=swap,
        free_disk_bytes=shutil.disk_usage(trace_root).free,
        trace_bytes=_tree_size(trace_root),
        temporary_bytes=_tree_size(temporary_root),
        jarvis_cpu_percent=cpu,
        jarvis_memory_bytes=memory,
        jarvis_pids=pids,
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
    trace_deltas = [
        later.trace_bytes - earlier.trace_bytes for earlier, later in pairwise(samples)
    ]
    if any(delta < 0 for delta in trace_deltas):
        failures.append("diagnostic traces were deleted during endurance")
    if any(delta > 16 * 1024**2 for delta in trace_deltas):
        failures.append("trace growth crossed the per-request reservation")
    settling = [item for item in samples if item.phase == "settling"]
    if settling[-1].used_swap_bytes > settling[0].used_swap_bytes:
        failures.append("swap continued to grow during settling")
    if settling[-1].temporary_bytes > samples[0].temporary_bytes:
        failures.append("temporary request data was not reclaimed")
    return tuple(failures)


def run(args: argparse.Namespace) -> int:
    required_run_seconds = REAL_RUN_SECONDS if args.external_workload else RUN_SECONDS
    if not args.smoke and (
        args.run_seconds != required_run_seconds
        or args.settling_seconds != SETTLING_SECONDS
        or args.sample_seconds != SAMPLE_SECONDS
    ):
        raise ValueError(
            f"acceptance timing must remain {required_run_seconds}s + 600s "
            "with 5s samples"
        )
    plan = () if args.external_workload else workload_plan()
    evidence = args.evidence.resolve()
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.touch(mode=0o600, exist_ok=False)
    samples: list[Sample] = []
    failures: list[str] = []
    requests_completed = 0
    started = time.monotonic()
    stop = threading.Event()

    def sample_loop() -> None:
        next_sample = started
        while not stop.is_set():
            phase = (
                "workload"
                if time.monotonic() - started < args.run_seconds
                else "settling"
            )
            try:
                sample = collect_sample(
                    phase=phase,
                    started=started,
                    trace_root=args.trace_root,
                    temporary_root=args.temporary_root,
                )
                samples.append(sample)
                with evidence.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps({"sample": asdict(sample)}, sort_keys=True) + "\n"
                    )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                failures.append(f"sampling failed: {type(exc).__name__}")
                stop.set()
                return
            next_sample += args.sample_seconds
            stop.wait(max(0.0, next_sample - time.monotonic()))

    sampler = threading.Thread(target=sample_loop, daemon=True)
    sampler.start()
    interval = args.run_seconds / len(plan) if plan else 0
    for index, (kind, nodeid) in enumerate(plan, start=1):
        due = started + (index - 1) * interval
        if not args.smoke:
            time.sleep(max(0.0, due - time.monotonic()))
        completed = subprocess.run(
            [str(args.python), "-m", "pytest", "-q", nodeid],
            cwd=args.source_root,
            check=False,
            capture_output=True,
            text=True,
        )
        record = {
            "request": index,
            "kind": kind,
            "nodeid": nodeid,
            "exit_code": completed.returncode,
        }
        with evidence.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        if completed.returncode != 0:
            failures.append(f"controlled request {index} failed")
            break
        requests_completed += 1
        if args.smoke:
            break
    if not failures:
        time.sleep(max(0.0, started + args.run_seconds - time.monotonic()))
        time.sleep(args.settling_seconds)
    stop.set()
    sampler.join(timeout=args.sample_seconds + 5)
    failures.extend(
        validate_samples(
            samples,
            run_seconds=args.run_seconds,
            settling_seconds=args.settling_seconds,
            sample_seconds=args.sample_seconds,
        )
    )
    summary = {
        "mode": "external" if args.external_workload else "controlled",
        "requests_planned": len(plan),
        "requests_completed": requests_completed,
        "samples": len(samples),
        "failures": failures,
    }
    with evidence.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"summary": summary}, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return int(bool(failures))


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
