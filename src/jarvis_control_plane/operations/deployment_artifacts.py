"""Artifact lock and application-source validation for deployment bundles."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .deployment_models import DATABASE_SCHEMAS


def _memory_mib(value: str) -> int:
    return int(value.removesuffix("M"))


def _application_source_sha256(source_root: Path, errors: list[str]) -> str:
    paths = [source_root / "pyproject.toml", source_root / "README.md"]
    source_directory = source_root / "src"
    if source_directory.is_dir():
        paths.extend(sorted(source_directory.rglob("*.py")))
    if any(not path.is_file() for path in paths) or len(paths) == 2:
        errors.append("application source tree is incomplete")
        return ""
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(source_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_artifacts(
    root: Path,
    lock: Mapping[str, Any],
    errors: list[str],
    *,
    source_root: Path,
    database_schemas: Mapping[str, str] = DATABASE_SCHEMAS,
    source_hash: Callable[[Path, list[str]], str] = _application_source_sha256,
) -> None:
    if set(lock) != {
        "schema_version",
        "application",
        "database_schemas",
        "python_base_image",
        "uv_build_image",
        "os_packages",
        "requirements_lock",
    }:
        errors.append("artifact lock has missing or unknown keys")
        return
    if lock.get("schema_version") != 1:
        errors.append("artifact lock schema_version must be 1")
    application = lock.get("application")
    if (
        not isinstance(application, Mapping)
        or application.get("name") != "jarvis-v2"
        or application.get("version") != "0.1.0"
        or not re.fullmatch(r"[0-9a-f]{40}", str(application.get("git_revision", "")))
    ):
        errors.append("application artifact must be pinned to a Git revision")
    elif application.get("source_sha256") != source_hash(source_root, errors):
        errors.append("application source differs from the pinned artifact")
    schemas = lock.get("database_schemas")
    if schemas != database_schemas:
        errors.append("database schema fingerprints must be complete and pinned")
    base = lock.get("python_base_image")
    reference = base.get("reference") if isinstance(base, Mapping) else None
    if not isinstance(reference, str) or not re.fullmatch(
        r"python:3\.13\.13-slim-bookworm@sha256:[0-9a-f]{64}", reference
    ):
        errors.append("Python base image must be pinned by tag and sha256 digest")
    uv_image = lock.get("uv_build_image")
    uv_reference = uv_image.get("reference") if isinstance(uv_image, Mapping) else None
    if not isinstance(uv_reference, str) or not re.fullmatch(
        r"ghcr\.io/astral-sh/uv:0\.6\.14@sha256:[0-9a-f]{64}", uv_reference
    ):
        errors.append("uv build image must be pinned by tag and sha256 digest")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    from_instructions = tuple(
        line.strip() for line in dockerfile.splitlines() if line.startswith("FROM ")
    )
    expected_from = (f"FROM {uv_reference} AS uv", f"FROM {reference}")
    if len(from_instructions) != 2 or not re.fullmatch(
        r"FROM python:3\.13\.13-slim-bookworm@sha256:[0-9a-f]{64}",
        from_instructions[-1] if from_instructions else "",
    ):
        errors.append("Dockerfile base image must be pinned by sha256 digest")
    elif from_instructions != expected_from:
        errors.append("Dockerfile images differ from artifact lock")
    if "RUN uv pip install" not in dockerfile or "RUN python -m pip" in dockerfile:
        errors.append("Dockerfile dependency installation must use uv")
    if (
        'ENTRYPOINT ["python", "-m", "jarvis_control_plane.service_runtime"]'
    ) not in dockerfile:
        errors.append("Dockerfile must enter the role-specific service runtime")
    if lock.get("os_packages") != {
        "git": "1:2.39.5-0+deb12u3",
        "openssh-client": "1:9.2p1-2+deb12u10",
    }:
        errors.append("vault operating-system packages differ from the artifact lock")
    if "ARG GIT_VERSION=1:2.39.5-0+deb12u3" not in dockerfile:
        errors.append("Dockerfile must pin the vault Git client package")
    if "ARG OPENSSH_CLIENT_VERSION=1:9.2p1-2+deb12u10" not in dockerfile:
        errors.append("Dockerfile must pin the vault SSH client package")
    if (
        '"git=${GIT_VERSION}"' not in dockerfile
        or '"openssh-client=${OPENSSH_CLIENT_VERSION}"' not in dockerfile
    ):
        errors.append("Dockerfile must install only the pinned vault clients")

    requirement = lock.get("requirements_lock")
    expected_hash = (
        requirement.get("sha256") if isinstance(requirement, Mapping) else None
    )
    if (
        not isinstance(requirement, Mapping)
        or requirement.get("path") != "requirements.lock"
    ):
        errors.append("requirements lock path must be requirements.lock")
    actual_hash = hashlib.sha256(
        (root / "requirements.lock").read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()
    if expected_hash != actual_hash:
        errors.append("requirements.lock digest differs from artifact lock")
