"""Contracts for the opt-in deployment probe's evidence, not live web access."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def probe():
    path = (
        Path(__file__).resolve().parents[2]
        / "deployment/personal-runtime/verify-web.py"
    )
    spec = importlib.util.spec_from_file_location("verify_web", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_failure_preserves_accumulated_evidence(probe, monkeypatch, tmp_path):
    async def fail(live, report):
        report["evidence"] = [{"outcome": "blocked", "results": []}]
        raise RuntimeError("profile cleanup failed")

    monkeypatch.setattr(probe, "verify", fail)
    output = tmp_path / "report.json"
    assert probe.main(["--live", "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert report["evidence"] == [{"outcome": "blocked", "results": []}]
    assert report["verification_error"] == "RuntimeError: profile cleanup failed"


def test_probe_follow_up_needs_evidence_in_runtime_transcript(probe):
    provider = probe.ScriptedProvider()
    provider.reuse_session = True
    page = {
        "state": "completed",
        "findings": "Value is 42. Source: https://example.com/",
    }
    source = {
        "type": "function_call_output",
        "output": json.dumps(page),
    }
    first = asyncio.run(provider.create({"input": [source]}, timeout=1))
    assert "Value is 42." in first.output_text
    # A broken runtime dropping history must not pass by using provider memory.
    missing = asyncio.run(provider.create({"input": []}, timeout=1))
    assert missing.output_text == "No source findings are available."
    assert provider.supporting_findings is None


def test_probe_failed_recheck_cannot_quote_previous_success(probe):
    provider = probe.ScriptedProvider()
    provider.request_output_start = 1
    old = {
        "type": "function_call_output",
        "output": json.dumps(
            {
                "state": "completed",
                "findings": "Old value. Source: https://example.com/",
            }
        ),
    }
    failed = {
        "type": "function_call_output",
        "output": json.dumps(
            {
                "state": "failed",
                "reason": "unavailable",
                "findings": "Old value. Source: https://example.com/",
            }
        ),
    }
    reply = asyncio.run(provider.create({"input": [old, failed]}, timeout=1))
    assert reply.output_text == "No source findings are available."
