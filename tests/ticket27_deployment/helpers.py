"""Shared fixtures and paths for Ticket 27 deployment-bundle tests."""

from __future__ import annotations

import shutil
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_BUNDLE = REPOSITORY_ROOT / "deployment"


def _copy_bundle(tmp_path: Path) -> Path:
    target = tmp_path / "deployment"
    shutil.copytree(SHIPPED_BUNDLE, target)
    return target


def _active_configuration(tmp_path: Path) -> Path:
    content = (SHIPPED_BUNDLE / "config.example.toml").read_text(encoding="utf-8")
    replacements = {
        'configuration_kind = "example"': 'configuration_kind = "active"',
        "example-operator-id": "operator-01",
        "example-internal-session-id": "openwa-session-01",
        "example-named-session": "openwa-named-01",
        "example-operator-conversation-id": "conversation-01",
        "example-google-subject": "operator@jarvis.invalid",
        "https://oauth.example.invalid/callback": "https://oauth.jarvis.invalid/callback",
        "example-windows-worker": "windows-01",
        "example-ubuntu-worker": "ubuntu-01",
        "ssh://vault.example.invalid/notes.git": "ssh://vault.jarvis.invalid/notes.git",
        'vault_hosts = ["vault.example.invalid"]': 'vault_hosts = ["vault.jarvis.invalid"]',
    }
    for old, new in replacements.items():
        content = content.replace(old, new)
    path = tmp_path / "jarvis.toml"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o444)
    return path
