from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "deployment" / "personal-runtime"


def test_replacement_package_has_valid_source_and_dependency_pins() -> None:
    entries = (PACKAGE / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    pinned_paths = set()
    for entry in entries:
        expected, relative_path = entry.split("  ", 1)
        pinned_paths.add(relative_path)
        assert (
            hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest() == expected
        )

    runtime_sources = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src" / "jarvis_personal_runtime").glob("*.py")
    }
    assert runtime_sources <= pinned_paths
    assert "pyproject.toml" in pinned_paths
    assert "deployment/personal-runtime/requirements.lock" in pinned_paths

    requirements = (PACKAGE / "requirements.lock").read_text(encoding="utf-8")
    for requirement in ("httpx==0.28.1", "openai==2.53.0", "tiktoken==0.14.0"):
        assert requirement in requirements
    assert "--hash=sha256:" in requirements


def test_native_service_runs_only_the_replacement_with_private_state() -> None:
    unit = (PACKAGE / "jarvis-personal-runtime.service").read_text(encoding="utf-8")

    assert "User=jarvis-personal-runtime" in unit
    assert "Group=jarvis-personal-runtime" in unit
    assert "UMask=0077" in unit
    assert (
        "ExecStartPre=/opt/jarvis-personal-runtime/current/.venv/bin/jarvis-personal-runtime --root /var/lib/jarvis-personal-runtime --check"
        in unit
    )
    assert (
        "ExecStart=/opt/jarvis-personal-runtime/current/.venv/bin/jarvis-personal-runtime --root /var/lib/jarvis-personal-runtime"
        in unit
    )
    assert "StandardOutput=journal" in unit
    assert "StandardError=journal" in unit
    assert "ProtectSystem=full" in unit
    assert "ProtectHome=" not in unit
    assert "ReadWritePaths=/var/lib/jarvis-personal-runtime" in unit
    assert "jarvis_control_plane" not in unit
    assert "EnvironmentFile=" not in unit


def test_example_configuration_carries_the_private_handoff_and_rotating_trace() -> None:
    config = tomllib.loads(
        (PACKAGE / "jarvis.toml.example").read_text(encoding="utf-8")
    )["runtime"]

    assert config["listener_host"] == "172.17.0.1"
    assert config["listener_port"] == 8787
    assert config["trace_path"] == "data/runtime-trace.jsonl"
    assert config["trace_max_bytes"] > 0
    assert config["message_cache_retention_days"] == 7
    assert config["openwa_api_base_url"].startswith("http://")


def test_runbook_keeps_validation_inactive_and_documents_operations() -> None:
    runbook = (PACKAGE / "README.md").read_text(encoding="utf-8")
    required = (
        "Validation without activation",
        "sha256sum --check deployment/personal-runtime/SHA256SUMS",
        "uv pip install --python .venv/bin/python --require-hashes",
        "chown root:jarvis-personal-runtime .env",
        "chmod 0440 .env",
        "chmod 0600 jarvis.toml SYSTEM.md",
        "systemctl start jarvis-personal-runtime",
        "systemctl stop jarvis-personal-runtime",
        "systemctl status jarvis-personal-runtime",
        "journalctl -u jarvis-personal-runtime",
        "Rollback",
        "Do not modify the live OpenWA project",
        "Do not change pairing state",
        "Do not change firewall rules",
        "previous runtime",
    )

    for text in required:
        assert text in runbook
