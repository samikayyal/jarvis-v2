from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from jarvis_personal_runtime.runtime import ApprovalRequired
from jarvis_personal_runtime.vault_git import (
    VaultGitSettings,
    VaultToolError,
    VaultTools,
)


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "vault.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    remote.mkdir()
    seed.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    _git(seed, "init", "--initial-branch=main")
    _git(seed, "config", "user.name", "Seed")
    _git(seed, "config", "user.email", "seed@example.test")
    _git(seed, "config", "core.autocrlf", "false")
    (seed / "Projects").mkdir()
    (seed / "Projects" / "Jarvis.md").write_text(
        "# Jarvis\r\n\r\nStatus: planning\r\n", encoding="utf-8", newline=""
    )
    (seed / ".hidden.md").write_text("hidden", encoding="utf-8")
    _git(seed, "add", "--", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "origin", "main")
    _git(
        tmp_path,
        "-c",
        "core.autocrlf=false",
        "clone",
        "--branch",
        "main",
        str(remote),
        str(clone),
    )
    _git(clone, "config", "core.autocrlf", "false")
    return remote, seed, clone


def _tool(clone: Path, remote: Path) -> VaultTools:
    return VaultTools(
        clone,
        VaultGitSettings(
            remote=str(remote), branch="main", note_directories=("Projects",)
        ),
    )


def _head(repository: Path) -> str:
    return _git(repository, "rev-parse", "HEAD")


def test_read_synchronizes_and_returns_revision(
    repositories: tuple[Path, Path, Path],
) -> None:
    remote, _seed, clone = repositories
    tool = _tool(clone, remote)

    result = json.loads(
        asyncio.run(
            tool.execute("read_vault", {"mode": "read", "value": "Projects/Jarvis.md"})
        )
    )

    assert result["freshness"] == "synced"
    assert result["base_revision"] == _head(clone)
    assert result["content"] == "# Jarvis\r\n\r\nStatus: planning\r\n"


def test_edit_requires_approval_and_pushes_exact_crlf_change(
    repositories: tuple[Path, Path, Path],
) -> None:
    remote, _seed, clone = repositories
    tool = _tool(clone, remote)
    before = json.loads(
        asyncio.run(
            tool.execute("read_vault", {"mode": "read", "value": "Projects/Jarvis.md"})
        )
    )

    proposed = asyncio.run(
        tool.execute(
            "edit_vault",
            {
                "base_revision": before["base_revision"],
                "commit_message": "jarvis: update status",
                "changes": [
                    {
                        "operation": "update",
                        "path": "Projects/Jarvis.md",
                        "replacements": [
                            {
                                "old_text": "Status: planning",
                                "new_text": "Status: started",
                            }
                        ],
                    }
                ],
            },
        )
    )

    assert isinstance(proposed, ApprovalRequired)
    assert proposed.action.allow_save_permission is False
    assert "Status: planning" in (clone / "Projects" / "Jarvis.md").read_text(
        encoding="utf-8"
    )
    result = json.loads(asyncio.run(tool.resume(proposed.continuation, approved=True)))

    assert result["status"] == "synced"
    assert result["commit"] == _head(clone)
    assert _git(remote, "rev-parse", "refs/heads/main") == result["commit"]
    assert (clone / "Projects" / "Jarvis.md").read_bytes() == (
        b"# Jarvis\r\n\r\nStatus: started\r\n"
    )
    assert _git(clone, "show", "-s", "--format=%an <%ae>", "HEAD") == (
        "Jarvis <jarvis@samikayyal.com>"
    )


def test_create_is_bounded_to_configured_directory_and_round_trips(
    repositories: tuple[Path, Path, Path],
) -> None:
    remote, _seed, clone = repositories
    tool = _tool(clone, remote)
    base = json.loads(
        asyncio.run(
            tool.execute("read_vault", {"mode": "read", "value": "Projects/Jarvis.md"})
        )
    )["base_revision"]
    proposed = asyncio.run(
        tool.execute(
            "edit_vault",
            {
                "base_revision": base,
                "commit_message": "jarvis: add note",
                "changes": [
                    {
                        "operation": "create",
                        "path": "Projects/New.md",
                        "content": "# New\n",
                    }
                ],
            },
        )
    )
    assert isinstance(proposed, ApprovalRequired)
    result = json.loads(asyncio.run(tool.resume(proposed.continuation, approved=True)))
    assert result["status"] == "synced"
    assert (clone / "Projects" / "New.md").read_text(encoding="utf-8") == "# New\n"


def test_revalidation_rejects_remote_change_without_writing(
    repositories: tuple[Path, Path, Path],
) -> None:
    remote, seed, clone = repositories
    tool = _tool(clone, remote)
    base = json.loads(
        asyncio.run(
            tool.execute("read_vault", {"mode": "read", "value": "Projects/Jarvis.md"})
        )
    )["base_revision"]
    proposed = asyncio.run(
        tool.execute(
            "edit_vault",
            {
                "base_revision": base,
                "commit_message": "jarvis: stale attempt",
                "changes": [
                    {
                        "operation": "update",
                        "path": "Projects/Jarvis.md",
                        "replacements": [{"old_text": "planning", "new_text": "stale"}],
                    }
                ],
            },
        )
    )
    assert isinstance(proposed, ApprovalRequired)
    (seed / "Projects" / "Jarvis.md").write_text("remote change\n", encoding="utf-8")
    _git(seed, "add", "--", "Projects/Jarvis.md")
    _git(seed, "commit", "-m", "remote change")
    _git(seed, "push", "origin", "main")

    result = json.loads(asyncio.run(tool.resume(proposed.continuation, approved=True)))

    assert result["status"] == "stale_base"
    assert "Status: planning" in (clone / "Projects" / "Jarvis.md").read_text(
        encoding="utf-8"
    )


def test_rejects_duplicate_or_overlapping_replacements(
    repositories: tuple[Path, Path, Path],
) -> None:
    remote, _seed, clone = repositories
    tool = _tool(clone, remote)
    base = json.loads(
        asyncio.run(
            tool.execute("read_vault", {"mode": "read", "value": "Projects/Jarvis.md"})
        )
    )["base_revision"]
    args = {
        "base_revision": base,
        "commit_message": "jarvis: invalid",
        "changes": [
            {
                "operation": "update",
                "path": "Projects/Jarvis.md",
                "replacements": [
                    {"old_text": "Status", "new_text": "State"},
                    {"old_text": "Status: planning", "new_text": "State: planning"},
                ],
            }
        ],
    }
    with pytest.raises(VaultToolError, match="overlap"):
        asyncio.run(tool.execute("edit_vault", args))
