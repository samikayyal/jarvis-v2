"""Native worker, systemd, and OpenWA handoff artifact validation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def _validate_handoff_description(path: Path, errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    required = (
        "future two-member private handoff network",
        "OpenWA remains independently deployed",
        "must not be created or attached by this bundle",
        "complete OpenWA verification ladder",
    )
    for phrase in required:
        if phrase not in text:
            errors.append(f"OpenWA handoff description is missing: {phrase}")


def _unit_directives(text: str) -> dict[tuple[str, str], tuple[str, ...]]:
    section = ""
    values: dict[tuple[str, str], list[str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        directive = values.setdefault((section, key), [])
        if not value:
            directive.clear()
        else:
            directive.append(value)
    return {key: tuple(value) for key, value in values.items()}


def _validate_backup_units(
    root: Path,
    errors: list[str],
    *,
    unit_parser: Callable[
        [str], dict[tuple[str, str], tuple[str, ...]]
    ] = _unit_directives,
) -> None:
    service = unit_parser((root / "jarvis-backup.service").read_text(encoding="utf-8"))
    timer = unit_parser((root / "jarvis-backup.timer").read_text(encoding="utf-8"))
    expected_service = {
        ("Unit", "Description"): ("Create the nightly Jarvis administrative backup",),
        ("Service", "Type"): ("oneshot",),
        ("Service", "User"): ("root",),
        ("Service", "UMask"): ("0077",),
        ("Service", "WorkingDirectory"): ("/opt/jarvis/current",),
        ("Service", "Environment"): ("PYTHONPATH=/opt/jarvis/current/src",),
        ("Service", "ExecStart"): (
            (
                "/opt/jarvis/current/.venv/bin/python -m "
                "jarvis_control_plane.administrative_backup create --kind nightly "
                "--artifact-lock /opt/jarvis/current/deployment/artifacts.lock.json "
                "--compose-manifest /opt/jarvis/current/deployment/compose.yaml "
                "--image-digests /etc/jarvis/image-digests.json"
            ),
        ),
    }
    expected_timer = {
        ("Unit", "Description"): ("Run the Jarvis administrative backup nightly",),
        ("Timer", "OnCalendar"): ("*-*-* 02:00:00 UTC",),
        ("Timer", "Persistent"): ("true",),
        ("Timer", "RandomizedDelaySec"): ("15m",),
        ("Timer", "Unit"): ("jarvis-backup.service",),
        ("Install", "WantedBy"): ("timers.target",),
    }
    if service != expected_service:
        errors.append("nightly backup service differs from the reviewed directives")
    if timer != expected_timer:
        errors.append("nightly backup timer differs from the reviewed directives")


def _validate_native_worker_artifacts(
    root: Path,
    errors: list[str],
    *,
    unit_parser: Callable[
        [str], dict[tuple[str, str], tuple[str, ...]]
    ] = _unit_directives,
) -> None:
    unit = unit_parser(
        (root / "systemd/jarvis-ubuntu-worker.service").read_text(encoding="utf-8")
    )
    required_unit = {
        ("Service", "User"): ("jarvis-worker",),
        ("Service", "Group"): ("jarvis-worker",),
        ("Service", "UMask"): ("0077",),
        ("Service", "RuntimeDirectory"): ("jarvis-worker",),
        ("Service", "NoNewPrivileges"): ("yes",),
        ("Service", "RestrictAddressFamilies"): ("AF_UNIX",),
        ("Service", "Restart"): ("on-failure",),
    }
    if any(unit.get(key) != value for key, value in required_unit.items()):
        errors.append(
            "native Ubuntu worker unit differs from reviewed security directives"
        )
    exec_start = unit.get(("Service", "ExecStart"), ())
    expected_exec_start = (
        "/opt/jarvis/current/.venv/bin/python -m "
        "jarvis_control_plane.native_worker_runtime ubuntu --config "
        "/etc/jarvis/native/ubuntu-worker.json"
    )
    if exec_start != (expected_exec_start,):
        errors.append(
            "native Ubuntu worker command differs from the reviewed entrypoint"
        )

    installer = (root / "windows/install-jarvis-worker.ps1").read_text(encoding="utf-8")
    required_windows_markers = (
        "$serviceName = 'JarvisWindowsWorker'",
        "'NT AUTHORITY\\LOCAL SERVICE'",
        "windows-service --config",
        "-StartupType Manual",
    )
    if any(marker not in installer for marker in required_windows_markers):
        errors.append(
            "native Windows worker installer differs from reviewed boundaries"
        )

    readme = (root / "README.md").read_text(encoding="utf-8")
    required_runtime_markers = (
        "/opt/jarvis/python/cpython-3.13.13-linux-x86_64-gnu/bin/python3.13",
        'uv venv --python "$JARVIS_HOST_PYTHON"',
        "systemd-run --wait --collect --unit=jarvis-python-mdwe-preflight",
        "--property=MemoryDenyWriteExecute=yes",
    )
    if any(marker not in readme for marker in required_runtime_markers):
        errors.append(
            "native host runtime installation is not pinned and hardening-preflighted"
        )
