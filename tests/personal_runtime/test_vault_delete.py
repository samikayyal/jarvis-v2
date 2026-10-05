"""Deletion uses the same frozen, synchronized vault boundary as note editing."""

from __future__ import annotations

import asyncio
import json

import pytest

from jarvis_personal_runtime.permissions import TomlPermissionStore
from jarvis_personal_runtime.runtime import ApprovalRequired
from jarvis_personal_runtime.vault import ReadVaultTool
from jarvis_personal_runtime.vault_git import (
    VaultGitSettings,
    VaultToolError,
    VaultTools,
)

from .test_vault_regressions import edit, git, vault

__all__ = ["vault"]


def delete_args(clone, path="note.md"):
    return {
        "base_revision": git(clone, "rev-parse", "HEAD"),
        "commit_message": "jarvis: delete note",
        "path": path,
    }


@pytest.mark.parametrize("empty", [False, True])
def test_delete_previews_and_syncs_one_deletion_with_history(vault, empty):
    clone, remote, tool = vault
    if empty:
        (clone / "note.md").write_bytes(b"")
        git(clone, "add", "note.md")
        git(
            clone,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.test",
            "commit",
            "-m",
            "Empty note",
        )
        git(clone, "push", "origin", "main")
    before = (clone / "note.md").read_bytes()
    base = git(clone, "rev-parse", "HEAD")
    step = asyncio.run(tool.execute("delete_vault", delete_args(clone)))
    assert isinstance(step, ApprovalRequired)
    assert "Delete this entire note:" in step.action.display
    assert (before.decode() or "(empty note)") in step.action.display
    assert not step.action.allow_save_permission
    assert (clone / "note.md").read_bytes() == before
    result = json.loads(asyncio.run(tool.resume(step.continuation, approved=True)))
    assert result["status"] == "synced"
    assert result["paths"] == ["note.md"]
    assert not (clone / "note.md").exists()
    assert git(clone, "status", "--porcelain") == ""
    assert git(remote, "rev-parse", "main") == git(clone, "rev-parse", "HEAD")
    assert git(remote, "ls-tree", "--name-only", "main") == ""
    assert git(clone, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert git(remote, "show", f"{base}:note.md") == before.decode().strip()
    assert (
        json.loads(asyncio.run(tool.resume(step.continuation, approved=True)))["status"]
        == "already_resolved"
    )


def test_delete_rejection_preserves_note_and_cannot_be_replayed(vault):
    clone, remote, tool = vault
    base = git(remote, "rev-parse", "main")
    step = asyncio.run(tool.execute("delete_vault", delete_args(clone)))
    result = json.loads(asyncio.run(tool.resume(step.continuation, approved=False)))
    assert result["status"] == "rejected"
    assert (
        json.loads(asyncio.run(tool.resume(step.continuation, approved=True)))["status"]
        == "already_resolved"
    )
    assert (clone / "note.md").read_bytes() == b"aaa\n"
    assert git(remote, "rev-parse", "main") == base


def test_saved_edit_permission_does_not_authorize_deletion(vault, tmp_path):
    clone, _, original = vault
    store = TomlPermissionStore(tmp_path / "jarvis.toml")
    tool = VaultTools(clone, original._settings, permission_store=store)
    step = asyncio.run(
        tool.execute(
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
    )
    store.add(step.action.host, step.action.prefix)
    deletion = asyncio.run(tool.execute("delete_vault", delete_args(clone)))
    assert isinstance(deletion, ApprovalRequired)
    assert not deletion.action.allow_save_permission
    result = json.loads(asyncio.run(tool.resume(deletion.continuation, approved=True)))
    assert result["approval"] == {
        "decision": "approved_once",
        "preview_shown": True,
        "source": "operator_reply",
    }


@pytest.mark.parametrize("change", ["local", "remote", "offline"])
def test_delete_revalidates_before_removing_note(vault, tmp_path, change):
    clone, remote, tool = vault
    step = asyncio.run(tool.execute("delete_vault", delete_args(clone)))
    if change == "local":
        (clone / "note.md").write_bytes(b"changed\n")
    elif change == "remote":
        other = tmp_path / "other"
        git(tmp_path, "clone", str(remote), str(other))
        (other / "note.md").write_bytes(b"changed\n")
        git(other, "add", "note.md")
        git(
            other,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.test",
            "commit",
            "-m",
            "Remote change",
        )
        git(other, "push", "origin", "main")
    else:
        remote.rename(tmp_path / "offline.git")
    result = json.loads(asyncio.run(tool.resume(step.continuation, approved=True)))
    assert (
        result["status"]
        == {
            "local": "execution_failed",
            "remote": "stale_base",
            "offline": "sync_unavailable",
        }[change]
    )
    assert (clone / "note.md").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../note.md",
        "/note.md",
        ".obsidian/config.md",
        "note.txt",
        "missing.md",
        "note.md/child.md",
    ],
)
def test_delete_rejects_unsafe_or_missing_paths(vault, path):
    clone, _, tool = vault
    with pytest.raises(VaultToolError):
        asyncio.run(tool.execute("delete_vault", delete_args(clone, path)))
    assert (clone / "note.md").read_bytes() == b"aaa\n"


def test_delete_rejects_paths_outside_configured_directories(vault):
    clone, remote, _ = vault
    tool = VaultTools(
        clone,
        VaultGitSettings(
            remote=str(remote), branch="main", note_directories=("Projects",)
        ),
    )
    with pytest.raises(VaultToolError, match="outside"):
        asyncio.run(tool.execute("delete_vault", delete_args(clone)))


def test_delete_contract_and_edit_cannot_smuggle_deletion(vault):
    clone, _, tool = vault
    assert "delete_vault" in {definition["name"] for definition in tool.definitions}
    assert "delete_vault" not in {
        definition["name"] for definition in ReadVaultTool.definitions
    }
    with pytest.raises(VaultToolError, match="arguments must be exactly"):
        asyncio.run(tool.execute("delete_vault", {**delete_args(clone), "extra": True}))
    with pytest.raises(VaultToolError, match="operation and path"):
        asyncio.run(
            tool.execute(
                "edit_vault", edit(clone, {"operation": "delete", "path": "note.md"})
            )
        )
