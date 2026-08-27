from __future__ import annotations

import argparse
import json
from pathlib import Path

from jarvis_control_plane import ticket33_endurance


def test_smoke_holds_the_complete_workload_and_settling_windows(
    monkeypatch, tmp_path: Path
) -> None:
    clock = {"value": 0.0}
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock["value"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["value"] += seconds

    class ImmediateThread:
        def __init__(self, *, target: object, daemon: bool) -> None:
            del target, daemon

        def start(self) -> None:
            return None

        def join(self, *, timeout: float) -> None:
            del timeout

    monkeypatch.setattr(ticket33_endurance.time, "monotonic", monotonic)
    monkeypatch.setattr(ticket33_endurance.time, "sleep", sleep)
    monkeypatch.setattr(ticket33_endurance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        ticket33_endurance.subprocess,
        "run",
        lambda *args, **kwargs: argparse.Namespace(returncode=0),
    )
    monkeypatch.setattr(
        ticket33_endurance, "validate_samples", lambda *args, **kwargs: ()
    )
    evidence = tmp_path / "evidence.jsonl"
    args = argparse.Namespace(
        smoke=True,
        run_seconds=10,
        settling_seconds=5,
        sample_seconds=5,
        evidence=evidence,
        source_root=tmp_path,
        python=Path("python"),
        trace_root=tmp_path,
        temporary_root=tmp_path,
        external_workload=False,
    )

    assert ticket33_endurance.run(args) == 0
    assert sleeps == [10.0, 5]
    summary = json.loads(evidence.read_text(encoding="utf-8").splitlines()[-1])
    assert summary["summary"]["requests_completed"] == 1


def test_external_workload_samples_without_running_a_controlled_request(
    monkeypatch, tmp_path: Path
) -> None:
    clock = {"value": 0.0}
    sleeps: list[float] = []

    monkeypatch.setattr(ticket33_endurance.time, "monotonic", lambda: clock["value"])

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["value"] += seconds

    class ImmediateThread:
        def __init__(self, *, target: object, daemon: bool) -> None:
            del target, daemon

        def start(self) -> None:
            return None

        def join(self, *, timeout: float) -> None:
            del timeout

    monkeypatch.setattr(ticket33_endurance.time, "sleep", sleep)
    monkeypatch.setattr(ticket33_endurance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        ticket33_endurance.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("external workload must not run pytest")
        ),
    )
    monkeypatch.setattr(
        ticket33_endurance, "validate_samples", lambda *args, **kwargs: ()
    )
    evidence = tmp_path / "external.jsonl"
    args = argparse.Namespace(
        smoke=True,
        external_workload=True,
        run_seconds=10,
        settling_seconds=5,
        sample_seconds=5,
        evidence=evidence,
        source_root=tmp_path,
        python=Path("python"),
        trace_root=tmp_path,
        temporary_root=tmp_path,
    )

    assert ticket33_endurance.run(args) == 0
    assert sleeps == [10.0, 5]
    summary = json.loads(evidence.read_text(encoding="utf-8").splitlines()[-1])
    assert summary["summary"] == {
        "failures": [],
        "mode": "external",
        "requests_completed": 0,
        "requests_planned": 0,
        "samples": 0,
    }
