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


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def vault(tmp_path: Path) -> tuple[Path, Path, VaultTools]:
    remote = tmp_path / "remote.git"
    clone = tmp_path / "vault"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(tmp_path, "clone", str(remote), str(clone))
    (clone / "note.md").write_bytes(b"aaa\n")
    git(clone, "add", "note.md")
    git(
        clone,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=f@example.com",
        "commit",
        "-m",
        "Initial",
    )
    git(clone, "push", "origin", "main")
    tool = VaultTools(
        clone,
        VaultGitSettings(remote=str(remote), branch="main", note_directories=(".",)),
    )
    return clone, remote, tool


def edit(clone: Path, change: dict[str, object]) -> dict[str, object]:
    return {
        "base_revision": git(clone, "rev-parse", "HEAD"),
        "commit_message": "jarvis: edit note",
        "changes": [change],
    }


def test_overlapping_occurrences_are_ambiguous(
    vault: tuple[Path, Path, VaultTools],
) -> None:
    clone, _, tool = vault
    with pytest.raises(VaultToolError, match="exactly once"):
        asyncio.run(
            tool.execute(
                "edit_vault",
                edit(
                    clone,
                    {
                        "operation": "update",
                        "path": "note.md",
                        "replacements": [{"old_text": "aa", "new_text": "b"}],
                    },
                ),
            )
        )
    assert (clone / "note.md").read_bytes() == b"aaa\n"


@pytest.mark.parametrize("content", ["", "# New note\n"])
def test_create_new_directory_and_empty_note_are_real_changes(
    vault: tuple[Path, Path, VaultTools],
    content: str,
) -> None:
    clone, remote, tool = vault

    async def scenario() -> None:
        step = await tool.execute(
            "edit_vault",
            edit(
                clone,
                {
                    "operation": "create",
                    "path": "Ideas/new.md",
                    "content": content,
                },
            ),
        )
        assert isinstance(step, ApprovalRequired)
        assert not (clone / "Ideas").exists()
        result = json.loads(await tool.resume(step.continuation, approved=True))
        assert result["status"] == "synced"

    asyncio.run(scenario())
    assert (clone / "Ideas/new.md").read_bytes() == content.encode()
    assert git(remote, "rev-parse", "main") == git(clone, "rev-parse", "HEAD")


def test_final_newline_change_is_visible_in_preview(
    vault: tuple[Path, Path, VaultTools],
) -> None:
    clone, _, tool = vault
    step = asyncio.run(
        tool.execute(
            "edit_vault",
            edit(
                clone,
                {
                    "operation": "update",
                    "path": "note.md",
                    "replacements": [{"old_text": "aaa\n", "new_text": "aaa"}],
                },
            ),
        )
    )
    assert isinstance(step, ApprovalRequired)
    assert "Replace:" not in step.action.display
    assert "newline" in step.action.display.lower()


def test_git_subdirectory_cannot_impersonate_vault_root(
    vault: tuple[Path, Path, VaultTools],
) -> None:
    clone, remote, _ = vault
    child = clone / "subfolder"
    child.mkdir()
    with pytest.raises((VaultToolError, ValueError)):
        tool = VaultTools(
            child,
            VaultGitSettings(
                remote=str(remote), branch="main", note_directories=(".",)
            ),
        )
        asyncio.run(tool.execute("read_vault", {"mode": "search", "value": "aaa"}))


def test_rejection_and_frozen_proposal(vault: tuple[Path, Path, VaultTools]) -> None:
    clone, remote, tool = vault
    base = git(remote, "rev-parse", "main")
    arguments = edit(
        clone,
        {
            "operation": "update",
            "path": "note.md",
            "replacements": [{"old_text": "aaa", "new_text": "approved"}],
        },
    )

    async def scenario() -> None:
        rejected = await tool.execute("edit_vault", arguments)
        assert isinstance(rejected, ApprovalRequired)
        await tool.resume(rejected.continuation, approved=False)
        assert (
            json.loads(await tool.resume(rejected.continuation, approved=True))[
                "status"
            ]
            == "already_resolved"
        )
        assert git(remote, "rev-parse", "main") == base
        proposal = await tool.execute("edit_vault", arguments)
        assert isinstance(proposal, ApprovalRequired)
        arguments["changes"][0]["replacements"][0]["new_text"] = "unapproved"
        assert (
            json.loads(await tool.resume(proposal.continuation, approved=True))[
                "status"
            ]
            == "synced"
        )

    asyncio.run(scenario())
    assert (clone / "note.md").read_bytes() == b"approved\n"


def test_offline_reads_disclose_staleness_and_writes_stop(
    vault: tuple[Path, Path, VaultTools],
) -> None:
    clone, remote, tool = vault

    async def scenario() -> None:
        fresh = json.loads(
            await tool.execute("read_vault", {"mode": "read", "value": "note.md"})
        )
        remote.rename(remote.with_name("offline.git"))
        stale = json.loads(
            await tool.execute("read_vault", {"mode": "read", "value": "note.md"})
        )
        assert fresh["freshness"] == "synced"
        assert stale["freshness"] == "stale" and stale["last_successful_sync"]
        assert stale["content"] == "aaa\n"
        with pytest.raises(VaultToolError):
            await tool.execute(
                "edit_vault",
                edit(
                    clone, {"operation": "create", "path": "new.md", "content": "new"}
                ),
            )
        assert not (clone / "new.md").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["rejected", "timeout_after_success", "unknown"])
def test_push_outcomes_preserve_one_commit_and_never_retry(
    vault: tuple[Path, Path, VaultTools],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from jarvis_personal_runtime.vault_git import _GitResult

    clone, remote, tool = vault
    base = git(clone, "rev-parse", "HEAD")
    original = tool._run_git
    attempts = 0

    async def transport(arguments: list[str], **kwargs: object) -> _GitResult:
        nonlocal attempts
        if arguments[0] == "push":
            attempts += 1
            if failure == "timeout_after_success":
                await original(arguments, **kwargs)
            return _GitResult(
                None if failure != "rejected" else 1,
                "",
                "",
                timed_out=failure != "rejected",
            )
        if failure == "unknown" and arguments[0] == "ls-remote":
            return _GitResult(1, "", "")
        return await original(arguments, **kwargs)

    monkeypatch.setattr(tool, "_run_git", transport)

    async def scenario() -> dict[str, object]:
        step = await tool.execute(
            "edit_vault",
            edit(
                clone,
                {
                    "operation": "update",
                    "path": "note.md",
                    "replacements": [{"old_text": "aaa", "new_text": "bbb"}],
                },
            ),
        )
        assert isinstance(step, ApprovalRequired)
        result = json.loads(await tool.resume(step.continuation, approved=True))
        assert (
            json.loads(await tool.resume(step.continuation, approved=True))["status"]
            == "already_resolved"
        )
        return result

    result = asyncio.run(scenario())
    assert (
        result["status"]
        == {
            "rejected": "committed_not_synced",
            "timeout_after_success": "synced",
            "unknown": "sync_unknown",
        }[failure]
    )
    assert attempts == 1
    assert git(clone, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert "commit" not in result
    assert (git(remote, "rev-parse", "main") == git(clone, "rev-parse", "HEAD")) == (
        failure == "timeout_after_success"
    )
