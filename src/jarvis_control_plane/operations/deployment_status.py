"""Administrative deployment status collection and backup freshness checks."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .deployment_models import RESOURCE_LIMITS


def _compose_json_rows(output: str) -> list[object]:
    try:
        rows = json.loads(output or "[]")
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    if isinstance(rows, Mapping):
        return [rows]
    if not isinstance(rows, list):
        raise TypeError("Compose output is not a list")
    return rows


def _backup_freshness(root: Path, *, now: datetime | None = None) -> str:
    try:
        manifests = [
            manifest
            for manifest in root.glob("*/manifest.json")
            if not manifest.parent.name.startswith(".partial-")
        ]
        if not manifests:
            return "missing"
        created = max(
            datetime.fromisoformat(
                json.loads(manifest.read_text(encoding="utf-8"))["created_at"]
            )
            for manifest in manifests
        )
        if created.tzinfo is None:
            raise ValueError
        age = (now or datetime.now(UTC)).astimezone(UTC) - created.astimezone(UTC)
        return "current" if timedelta(0) <= age <= timedelta(hours=36) else "stale"
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "invalid"


def administrative_status(
    bundle: str | Path,
    *,
    activation_override: str | Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    backup_root: str | Path = "/var/backups/jarvis",
    now: datetime | None = None,
    resource_limits: Mapping[str, object] = RESOURCE_LIMITS,
    compose_json_rows: Callable[[str], list[object]] = _compose_json_rows,
    backup_freshness: Callable[..., str] = _backup_freshness,
) -> dict[str, object]:
    """Combine local Compose health with authenticated dependency status."""

    compose = Path(bundle).resolve() / "compose.yaml"
    override = Path(activation_override).resolve()
    if not override.is_file():
        raise RuntimeError("administrative status activation override is unavailable")
    base = [
        "docker",
        "compose",
        "--file",
        str(compose),
        "--file",
        str(override),
        "--profile",
        "manual-activation",
    ]
    try:
        observed = runner(
            [*base, "ps", "--all", "--format", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
        rows = compose_json_rows(observed.stdout)
        if not rows or any(not isinstance(row, Mapping) for row in rows):
            raise TypeError("Compose status inventory is invalid")
        service_names = [row.get("Service") for row in rows]
        if (
            any(not isinstance(service, str) for service in service_names)
            or len(service_names) != len(set(service_names))
            or set(service_names) != set(resource_limits)
        ):
            raise TypeError("Compose status inventory is incomplete or ambiguous")
        by_service = {
            row.get("Service"): row for row in rows if isinstance(row, Mapping)
        }
        components = {
            service: (
                "ready"
                if by_service.get(service, {}).get("State") == "running"
                and by_service.get(service, {}).get("Health") in {"", "healthy"}
                else "unavailable"
            )
            for service in resource_limits
        }
        dependency = runner(
            [
                *base,
                "exec",
                "--interactive=false",
                "-T",
                "capability_broker",
                "uv",
                "run",
                "--no-project",
                "python",
                "-m",
                "jarvis_control_plane.service_runtime",
                "admin-status",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        details = next(
            json.loads(line)
            for line in reversed(dependency.stdout.splitlines())
            if line.startswith("{")
        )
        if not isinstance(details, dict):
            raise TypeError("dependency status is not an object")
    except (
        OSError,
        StopIteration,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        raise RuntimeError("administrative status is unavailable") from exc
    details["backup_freshness"] = backup_freshness(Path(backup_root), now=now)
    return {"components": components, **details}
