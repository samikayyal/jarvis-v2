"""Offline verification for the unactivated Jarvis deployment bundle.

This module is the compatibility implementation facade for deployment
validation.  The contract-specific checks live in cohesive sibling modules;
the wrappers here intentionally resolve through this module so historical
imports and monkeypatches keep their old behavior.
"""

from __future__ import annotations

import argparse
import hashlib  # noqa: F401 - retained as a historical module attribute
import json
import re  # noqa: F401 - retained as a historical module attribute
import subprocess
import tomllib  # noqa: F401 - retained as a historical module attribute
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass  # noqa: F401 - retained as a historical attribute
from datetime import UTC, datetime, timedelta  # noqa: F401 - compatibility imports
from decimal import Decimal, InvalidOperation  # noqa: F401 - compatibility imports
from pathlib import Path
from types import MappingProxyType  # noqa: F401 - compatibility import
from typing import Any
from urllib.parse import urlsplit  # noqa: F401 - compatibility import

import yaml  # noqa: F401 - retained as a historical module attribute

from ..acceptance_failpoints import reviewed_post_dispatch_failpoint_from_config
from ..knowledge_vault_writes import canonical_allowed_note_directories
from .deployment_artifacts import (
    _application_source_sha256 as _artifacts_application_source_sha256,
)
from .deployment_artifacts import _memory_mib as _artifacts_memory_mib
from .deployment_artifacts import (
    _validate_artifacts as _artifacts_validate,
)
from .deployment_compose import _validate_compose as _compose_validate
from .deployment_compose import (
    _validate_service_resources as _compose_validate_resources,
)
from .deployment_compose import (
    _validate_service_volumes as _compose_validate_volumes,
)
from .deployment_configuration import (
    CONFIG_KEYS,  # noqa: F401
    OPTIONAL_CONFIG_KEYS,  # noqa: F401
)
from .deployment_configuration import (
    _load_mapping as _configuration_load_mapping,
)
from .deployment_configuration import (
    _validate_configuration as _configuration_validate,
)
from .deployment_models import (
    ALLOWED_CREDENTIAL_MOUNTS,
    ALLOWED_PROTOCOL_MOUNTS,
    ALLOWED_STATE_MOUNTS,
    DATABASE_SCHEMAS,
    EXPECTED_IDENTITIES,
    REQUIRED_FILES,
    RESOURCE_LIMITS,
    BundleValidationError,
    BundleVerificationReport,
    ServiceResourceLimits,
)
from .deployment_native import (
    _unit_directives as _native_unit_directives,
)
from .deployment_native import (
    _validate_backup_units as _native_validate_backup_units,
)
from .deployment_native import (
    _validate_handoff_description as _native_validate_handoff_description,
)
from .deployment_native import (
    _validate_native_worker_artifacts as _native_validate_worker_artifacts,
)
from .deployment_status import (
    _backup_freshness as _status_backup_freshness,
)
from .deployment_status import (
    _compose_json_rows as _status_compose_json_rows,
)
from .deployment_status import (
    administrative_status as _status_administrative_status,
)


def _load_mapping(path: Path, errors: list[str], label: str) -> dict[str, Any]:
    return _configuration_load_mapping(path, errors, label)


def _validate_configuration(config: Mapping[str, Any], errors: list[str]) -> None:
    _configuration_validate(
        config,
        errors,
        expected_identities=EXPECTED_IDENTITIES,
        failpoint_parser=reviewed_post_dispatch_failpoint_from_config,
        note_directory_parser=canonical_allowed_note_directories,
    )


def validate_configuration(config: Mapping[str, Any]) -> None:
    """Validate one configuration document independently of bundle artifacts."""

    errors: list[str] = []
    _validate_configuration(config, errors)
    if errors:
        raise BundleValidationError(tuple(dict.fromkeys(errors)))


def _validate_artifacts(
    root: Path,
    lock: Mapping[str, Any],
    errors: list[str],
    *,
    source_root: Path,
) -> None:
    _artifacts_validate(
        root,
        lock,
        errors,
        source_root=source_root,
        database_schemas=DATABASE_SCHEMAS,
        source_hash=_application_source_sha256,
    )


def _validate_service_volumes(
    service: str,
    volumes: object,
    errors: list[str],
    *,
    credential_mounts: Mapping[str, frozenset[str]] = ALLOWED_CREDENTIAL_MOUNTS,
    protocol_mounts: Mapping[str, frozenset[str]] = ALLOWED_PROTOCOL_MOUNTS,
    state_mounts: Mapping[str, frozenset[str]] = ALLOWED_STATE_MOUNTS,
) -> None:
    _compose_validate_volumes(
        service,
        volumes,
        errors,
        credential_mounts=credential_mounts,
        protocol_mounts=protocol_mounts,
        state_mounts=state_mounts,
    )


def _validate_service_resources(
    service: str,
    raw: Mapping[str, Any],
    expected: ServiceResourceLimits,
    errors: list[str],
) -> None:
    _compose_validate_resources(service, raw, expected, errors)


def _validate_compose(
    compose: Mapping[str, Any], config: Mapping[str, Any], errors: list[str]
) -> bool:
    return _compose_validate(
        compose,
        config,
        errors,
        resource_limits=RESOURCE_LIMITS,
        credential_mounts=ALLOWED_CREDENTIAL_MOUNTS,
        protocol_mounts=ALLOWED_PROTOCOL_MOUNTS,
        state_mounts=ALLOWED_STATE_MOUNTS,
        validate_volumes=_validate_service_volumes,
        validate_resources=_validate_service_resources,
    )


def _validate_handoff_description(path: Path, errors: list[str]) -> None:
    _native_validate_handoff_description(path, errors)


def _unit_directives(text: str) -> dict[tuple[str, str], tuple[str, ...]]:
    return _native_unit_directives(text)


def _validate_backup_units(root: Path, errors: list[str]) -> None:
    _native_validate_backup_units(root, errors, unit_parser=_unit_directives)


def _validate_native_worker_artifacts(root: Path, errors: list[str]) -> None:
    _native_validate_worker_artifacts(root, errors, unit_parser=_unit_directives)


def _memory_mib(value: str) -> int:
    return _artifacts_memory_mib(value)


def _application_source_sha256(source_root: Path, errors: list[str]) -> str:
    return _artifacts_application_source_sha256(source_root, errors)


def _compose_json_rows(output: str) -> list[object]:
    return _status_compose_json_rows(output)


def verify_bundle(
    bundle: str | Path,
    *,
    configuration: str | Path | None = None,
    source_root: str | Path | None = None,
) -> BundleVerificationReport:
    """Validate one bundle without invoking any external program or service."""

    root = Path(bundle).resolve()
    errors: list[str] = []
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            errors.append(f"missing bundle file: {relative}")
    if errors:
        raise BundleValidationError(errors)

    compose = _load_mapping(root / "compose.yaml", errors, "compose")
    config_path = (
        Path(configuration).resolve()
        if configuration is not None
        else root / "config.example.toml"
    )
    config = _load_mapping(config_path, errors, "configuration")
    lock = _load_mapping(root / "artifacts.lock.json", errors, "artifact lock")

    _validate_configuration(config, errors)
    _validate_artifacts(
        root,
        lock,
        errors,
        source_root=(
            Path(source_root).resolve()
            if source_root is not None
            else root.parent.resolve()
        ),
    )
    handoff_active = _validate_compose(compose, config, errors)
    _validate_handoff_description(root / "openwa-handoff.md", errors)
    _validate_backup_units(root / "systemd", errors)
    _validate_native_worker_artifacts(root, errors)

    if errors:
        raise BundleValidationError(tuple(dict.fromkeys(errors)))

    services = tuple(sorted(RESOURCE_LIMITS))
    return BundleVerificationReport(
        release_id=str(config["release_id"]),
        services=services,
        aggregate_memory_mib=sum(
            _memory_mib(limit.memory) for limit in RESOURCE_LIMITS.values()
        ),
        aggregate_cpus=float(sum(limit.cpus for limit in RESOURCE_LIMITS.values())),
        aggregate_pids=sum(limit.pids for limit in RESOURCE_LIMITS.values()),
        openwa_handoff_activated=handoff_active,
        checked_files=REQUIRED_FILES,
    )


def _backup_freshness(root: Path, *, now: datetime | None = None) -> str:
    return _status_backup_freshness(root, now=now)


def administrative_status(
    bundle: str | Path,
    *,
    activation_override: str | Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    backup_root: str | Path = "/var/backups/jarvis",
    now: datetime | None = None,
) -> dict[str, object]:
    """Combine local Compose health with authenticated dependency status."""

    return _status_administrative_status(
        bundle,
        activation_override=activation_override,
        runner=runner,
        backup_root=backup_root,
        now=now,
        resource_limits=RESOURCE_LIMITS,
        compose_json_rows=_compose_json_rows,
        backup_freshness=_backup_freshness,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="deployment bundle directory")
    parser.add_argument("--configuration", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--administrative-status", action="store_true")
    parser.add_argument("--activation-override", type=Path)
    parser.add_argument("--backup-root", type=Path, default=Path("/var/backups/jarvis"))
    args = parser.parse_args(argv)
    try:
        report = verify_bundle(
            args.bundle,
            configuration=args.configuration,
            source_root=args.source_root,
        )
    except BundleValidationError as exc:
        for error in exc.errors:
            print(f"ERROR: {error}")
        return 1
    if args.administrative_status:
        if args.activation_override is None:
            parser.error(
                "--activation-override is required with --administrative-status"
            )
        print(
            json.dumps(
                administrative_status(
                    args.bundle,
                    activation_override=args.activation_override,
                    backup_root=args.backup_root,
                ),
                sort_keys=True,
            )
        )
    else:
        print(
            f"verified {report.release_id}: {len(report.services)} services, "
            f"{report.aggregate_memory_mib} MiB, {report.aggregate_cpus:.2f} CPU, "
            f"{report.aggregate_pids} PIDs; activation unchanged"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
