from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jarvis_personal_runtime.vault import ReadVaultTool
from jarvis_personal_runtime.vault_git import VaultGitSettings, VaultTools
from jarvis_personal_runtime.vault_tree import list_tree

from .test_vault_git import _git, repositories

__all__ = ["repositories"]


def tree(tool, offset=0):
    return json.loads(asyncio.run(tool.execute("list_vault_tree", {"offset": offset})))


def test_tree_lists_nested_empty_directories_and_attachments_without_reading(tmp_path):
    (tmp_path / "Notes" / "Empty").mkdir(parents=True)
    (tmp_path / "Notes" / "عربي.md").write_bytes(b"\xff" * 100_000)
    (tmp_path / "Notes" / "image.png").write_bytes(b"binary")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "private.json").write_text("private")
    (tmp_path / ".env").write_text("secret")
    (tmp_path / "Nested" / ".git").mkdir(parents=True)
    (tmp_path / "Nested" / "note.md").write_text("nested")

    result = tree(ReadVaultTool(tmp_path))

    assert result == {
        "scope": ["."],
        "entries": [
            {"path": "Notes", "type": "directory"},
            {"path": "Notes/Empty", "type": "directory"},
            {"path": "Notes/image.png", "type": "file"},
            {"path": "Notes/عربي.md", "type": "file"},
        ],
        "total_entries": 4,
        "next_offset": None,
    }


@pytest.mark.parametrize("limit", [65_536, 400])
def test_tree_pagination_covers_every_path_once_beyond_search_limit(tmp_path, limit):
    for index in range(230):
        (tmp_path / f"note-{index:03}.md").touch()
    tool = ReadVaultTool(tmp_path, max_result_chars=limit)
    paths = []
    offset = 0
    while offset is not None:
        encoded = asyncio.run(tool.execute("list_vault_tree", {"offset": offset}))
        assert len(encoded) <= limit
        result = json.loads(encoded)
        assert result["total_entries"] == 230
        assert result["entries"]
        paths.extend(entry["path"] for entry in result["entries"])
        offset = result["next_offset"]
    assert paths == [f"note-{index:03}.md" for index in range(230)]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"offset": True},
        {"offset": -1},
        {"offset": 1.2},
        {"offset": "0"},
        {"offset": 0, "path": "../"},
    ],
)
def test_tree_rejects_invalid_arguments(tmp_path, arguments):
    with pytest.raises(ValueError, match="list_vault_tree"):
        asyncio.run(ReadVaultTool(tmp_path).execute("list_vault_tree", arguments))


def test_tree_empty_out_of_range_and_too_small_output(tmp_path):
    assert tree(ReadVaultTool(tmp_path))["entries"] == []
    with pytest.raises(ValueError, match="offset exceeds"):
        tree(ReadVaultTool(tmp_path), 1)
    (tmp_path / "note.md").touch()
    with pytest.raises(ValueError, match="result limit"):
        tree(ReadVaultTool(tmp_path, max_result_chars=20))


def test_nested_scope_excludes_siblings_and_deduplicates_overlapping_roots(tmp_path):
    (tmp_path / "Projects" / "Jarvis").mkdir(parents=True)
    (tmp_path / "Projects" / "Jarvis" / "note.md").touch()
    (tmp_path / "Projects" / "other.md").touch()
    (tmp_path / "outside.md").touch()
    result = json.loads(
        list_tree(tmp_path, ("Projects/Jarvis",), 0, max_result_chars=2000)
    )
    assert [entry["path"] for entry in result["entries"]] == [
        "Projects",
        "Projects/Jarvis",
        "Projects/Jarvis/note.md",
    ]
    result = json.loads(
        list_tree(tmp_path, ("Projects", "Projects/Jarvis"), 0, max_result_chars=2000)
    )
    assert result["total_entries"] == 4


def test_symlink_ancestor_cannot_expose_an_outside_configured_directory(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "Notes").mkdir(parents=True)
    (outside / "Notes" / "secret.md").touch()
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
        (root / "linked.md").symlink_to(outside / "Notes" / "secret.md")
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert tree(ReadVaultTool(root))["entries"] == []
    result = json.loads(list_tree(root, ("linked/Notes",), 0, max_result_chars=2000))
    assert result["entries"] == []


def test_tree_fails_explicitly_on_scan_error_or_budget(tmp_path, monkeypatch):
    import jarvis_personal_runtime.vault_tree as module

    (tmp_path / "note.md").touch()
    monkeypatch.setattr(module, "_MAX_ENTRIES", 0)
    with pytest.raises(ValueError, match="entry limit"):
        tree(ReadVaultTool(tmp_path))

    def failed_walk(root, *, onerror, followlinks):
        onerror(PermissionError("denied"))
        return iter(())

    monkeypatch.setattr(module.os, "walk", failed_walk)
    with pytest.raises(ValueError, match="could not inspect"):
        tree(ReadVaultTool(tmp_path))


def test_git_tree_fetches_new_paths_preserves_scope_and_discloses_stale(repositories):
    remote, seed, clone = repositories
    tool = VaultTools(
        clone,
        VaultGitSettings(
            remote=str(remote), branch="main", note_directories=("Projects",)
        ),
    )
    (seed / "Projects" / "new.md").write_text("new")
    (seed / "outside.md").write_text("outside scope")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "New paths")
    _git(seed, "push", "origin", "main")
    base = _git(seed, "rev-parse", "HEAD")

    result = tree(tool)

    assert result["freshness"] == "synced"
    assert result["base_revision"] == base
    assert [entry["path"] for entry in result["entries"]] == [
        "Projects",
        "Projects/Jarvis.md",
        "Projects/new.md",
    ]
    assert _git(clone, "status", "--porcelain") == ""
    remote.rename(remote.with_name("offline.git"))
    stale = tree(tool)
    assert stale["freshness"] == "stale"
    assert stale["base_revision"] == base
    assert stale["entries"] == result["entries"]
    assert "warning" in stale and "last_successful_sync" in stale


def test_tree_routes_through_responses_collection(tmp_path: Path):
    from jarvis_personal_runtime.responses import (
        DirectResponsesRunner,
        PreparedToolCollection,
        ResponsesResult,
    )

    (tmp_path / "note.md").touch()

    class Provider:
        def __init__(self):
            self.calls = []

        async def create(self, request, *, timeout):
            self.calls.append(request)
            if len(self.calls) == 1:
                assert any(
                    tool["name"] == "list_vault_tree" for tool in request["tools"]
                )
                return ResponsesResult(
                    output=(
                        {
                            "type": "function_call",
                            "call_id": "tree",
                            "name": "list_vault_tree",
                            "arguments": '{"offset":0}',
                        },
                    ),
                    output_text="",
                )
            return ResponsesResult(output=(), output_text="note.md")

    provider = Provider()
    runner = DirectResponsesRunner(
        provider,
        tools=PreparedToolCollection(ReadVaultTool(tmp_path)),
        request_timeout_seconds=30,
    )
    result = asyncio.run(
        runner.run(
            "Show the vault tree",
            model="gpt-6-luna",
            reasoning="medium",
            system_prompt="Use prepared tools.",
        )
    )
    assert result.reply == "note.md"
    output = json.loads(provider.calls[-1]["input"][-1]["output"])
    assert output["entries"] == [{"path": "note.md", "type": "file"}]
