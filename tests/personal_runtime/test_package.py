from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "deployment" / "personal-runtime"


def _source_bytes(path: Path) -> bytes:
    """Match the LF bytes installed on the native Ubuntu target."""

    return path.read_bytes().replace(b"\r\n", b"\n")


def test_replacement_package_has_valid_source_and_dependency_pins() -> None:
    entries = (PACKAGE / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    pinned_paths = set()
    for entry in entries:
        expected, relative_path = entry.split("  ", 1)
        pinned_paths.add(relative_path)
        assert (
            hashlib.sha256(_source_bytes(ROOT / relative_path)).hexdigest() == expected
        )

    runtime_sources = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src" / "jarvis_personal_runtime").glob("*.py")
    }
    assert runtime_sources <= pinned_paths
    assert "pyproject.toml" in pinned_paths
    assert "deployment/personal-runtime/requirements.lock" in pinned_paths

    requirements = (PACKAGE / "requirements.lock").read_text(encoding="utf-8")
    for requirement in (
        "httpx==0.28.1",
        "openai==2.53.0",
        "setuptools==80.10.2",
        "tiktoken==0.14.0",
    ):
        assert requirement in requirements
    assert "--hash=sha256:" in requirements


def test_native_service_runs_only_the_replacement_with_private_state() -> None:
    unit = (PACKAGE / "jarvis-personal-runtime.service").read_text(encoding="utf-8")

    assert "User=@SERVICE_USER@" in unit
    assert "Group=@SERVICE_GROUP@" in unit
    assert "UMask=0077" in unit
    assert (
        "ExecStartPre=@RELEASE_ROOT@/current/.venv/bin/jarvis-personal-runtime --root @RUNTIME_ROOT@ --config /etc/jarvis/jarvis.toml --check"
        in unit
    )
    assert (
        "ExecStart=@RELEASE_ROOT@/current/.venv/bin/jarvis-personal-runtime --root @RUNTIME_ROOT@ --config /etc/jarvis/jarvis.toml"
        in unit
    )
    assert "StandardOutput=journal" in unit
    assert "StandardError=journal" in unit
    assert "ProtectSystem=full" in unit
    assert "ProtectHome=" not in unit
    assert "ReadWritePaths=@RUNTIME_ROOT@ /etc/jarvis/personal-runtime" in unit
    assert "jarvis_control_plane" not in unit
    assert "EnvironmentFile=" not in unit
    assert "/opt/jarvis-personal-runtime" not in unit
    assert "/var/lib/jarvis-personal-runtime" not in unit


def test_transactional_config_editor_validates_before_replacing_and_restarting() -> None:
    editor = (PACKAGE / "modify-jarvis-config").read_text(encoding="utf-8")

    assert "systemctl stop \"$service\"" in editor
    assert '"$editor" "$candidate"' in editor
    assert '--root "$runtime_root" --config "$candidate" --check' in editor
    assert 'mv -f -- "$candidate" "$config_target"' in editor
    assert "systemctl start \"$service\"" in editor
    assert '"$backup" "$config_target"' in editor


def test_example_configuration_carries_the_private_handoff_and_rotating_trace() -> None:
    config = tomllib.loads(
        (PACKAGE / "jarvis.toml.example").read_text(encoding="utf-8")
    )["runtime"]

    assert config["listener_host"] == "REPLACE_WITH_DOCKER_BRIDGE_GATEWAY"
    assert config["listener_port"] == 9011
    assert config["ubuntu_working_directory"] == "/srv/jarvis-workspace"
    assert config["trace_path"] == "data/runtime-trace.jsonl"
    assert config["trace_max_bytes"] > 0
    assert config["message_cache_retention_days"] == 7
    assert config["openwa_api_base_url"] == "REPLACE_WITH_PRIVATE_OPENWA_API_BASE_URL"


def test_runbook_documents_active_native_operations_without_legacy_fallback() -> None:
    runbook = (PACKAGE / "README.md").read_text(encoding="utf-8")
    required = (
        "Package verification",
        "/opt/jarvis-personal-runtime/releases/COMMIT",
        "/var/lib/jarvis-personal-runtime",
        "sha256sum --check deployment/personal-runtime/SHA256SUMS",
        "uv pip install --python .venv/bin/python --require-hashes",
        "`root:jarvis-personal-runtime`, `0440`",
        "`jarvis-personal-runtime:jarvis-personal-runtime`, `0600`",
        "systemctl start jarvis-personal-runtime",
        "systemctl stop jarvis-personal-runtime",
        "systemctl status jarvis-personal-runtime",
        "journalctl -u jarvis-personal-runtime",
        "Roll back only by stopping the service",
        "There is no legacy control-plane fallback",
        "Preserve `openwa-data`",
        "ufw allow in on BRIDGE_INTERFACE from OPENWA_CONTAINER_IP",
        "to BRIDGE_GATEWAY port PORT proto tcp",
        "Never allow the whole bridge subnet",
        "fresh QR",
        "LOGOUT",
    )

    for text in required:
        assert text in runbook


def test_only_personal_runtime_source_and_dependencies_remain() -> None:
    source_packages = {
        path.parent.name for path in (ROOT / "src").glob("*/__init__.py")
    }
    assert source_packages == {"jarvis_personal_runtime"}

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == [
        "httpx>=0.28.1",
        "openai==2.53.0",
        "tiktoken>=0.14.0",
    ]
