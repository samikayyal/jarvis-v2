"""Run and verify the fixed Ticket 33 controlled endurance workload."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

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
            "tests/ticket07/test_pending_actions.py::"
            "test_exact_approval_dispatches_the_frozen_payload_only_once"
        ),
    ),
    (
        "rejection",
        8,
        (
            "tests/ticket07/test_pending_actions.py::"
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
            "tests/ticket07/test_pending_actions.py::"
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


def workload_plan() -> tuple[tuple[str, str], ...]:
    plan: list[tuple[str, str]] = []
    for kind, count, nodeid in WORKLOADS:
        plan.extend((kind, nodeid) for _ in range(count))
    return tuple(plan)


from .endurance_support import (  # noqa: F401
    HostMeasurer,
    JsonlEvidenceWriter,
    ResourceMeasurement,
    Sample,
    Sampling,
    SubprocessWorkloadExecutor,
    SystemClock,
    SystemHostMeasurer,
    Timing,
    _docker_stats,
    _memory,
    _ThreadSampling,
    _trace_facts,
    _tree_size,
    collect_sample,
    validate_samples,
)


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
