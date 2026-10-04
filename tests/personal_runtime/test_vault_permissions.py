from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from jarvis_personal_runtime.permissions import (
    PermissionStoreError,
    TomlPermissionStore,
)
from jarvis_personal_runtime.responses import (
    DirectResponsesRunner,
    PreparedToolCollection,
    ResponsesResult,
)
from jarvis_personal_runtime.runtime import ApprovalRequired, PersonalRuntime
from jarvis_personal_runtime.vault_git import VaultToolError, VaultTools

from .test_runtime import inbound
from .test_vault_regressions import edit, git, vault

__all__ = ["vault"]


def update(clone: Path, old: str, new: str, path: str = "note.md") -> dict:
    return edit(
        clone,
        {
            "operation": "update",
            "path": path,
            "replacements": [{"old_text": old, "new_text": new}],
        },
    )


def test_whole_runtime_remembers_exact_file_across_restart_and_revokes(vault, tmp_path):
    clone, remote, original = vault
    store_path = tmp_path / "jarvis.toml"
    outputs = []
    events = []

    class Trace:
        def record(self, event, payload):
            events.append((event, payload))

    class Provider:
        def __init__(self, arguments):
            self.arguments = arguments
            self.called = False

        async def create(self, request, *, timeout):
            if not self.called:
                self.called = True
                return ResponsesResult(
                    output=(
                        {
                            "type": "function_call",
                            "call_id": "edit",
                            "name": "edit_vault",
                            "arguments": json.dumps(self.arguments),
                        },
                    ),
                    output_text="",
                )
            outputs.append(json.loads(request["input"][-1]["output"]))
            return ResponsesResult(output=(), output_text="Saved and synced.")

    def runtime(arguments):
        store = TomlPermissionStore(store_path)
        tool = VaultTools(
            clone, original._settings, permission_store=store, trace=Trace()
        )
        runner = DirectResponsesRunner(
            Provider(arguments),
            tools=PreparedToolCollection(tool),
            request_timeout_seconds=60,
        )
        return PersonalRuntime(request_runner=runner, permission_store=store)

    async def scenario():
        first = runtime(update(clone, "aaa", "bbb"))
        proposal = await first.receive(inbound("a", "Edit the note"))
        preview = proposal.replies[0]
        assert "Replace:\naaa\nWith:\nbbb" in preview
        assert "2 to always approve future edits to this file" in preview
        assert "commit" not in preview.lower() and "@@" not in preview
        assert git(clone, "rev-parse", "HEAD") not in preview
        assert (clone / "note.md").read_bytes() == b"aaa\n"
        await first.receive(inbound("b", "2"))
        assert outputs[-1]["approval"]["decision"] == "approved_and_saved"
        assert "commit" not in outputs[-1] and "base_revision" not in outputs[-1]
        await first.close()

        second = runtime(update(clone, "bbb", "ccc"))
        result = await second.receive(inbound("c", "Edit the note again"))
        assert result.disposition == "completed"
        assert outputs[-1]["approval"] == {
            "decision": "saved_file_permission",
            "preview_shown": False,
            "source": "saved_permission",
        }
        assert (clone / "note.md").read_bytes() == b"ccc\n"
        assert git(remote, "rev-parse", "main") == git(clone, "rev-parse", "HEAD")
        permission = TomlPermissionStore(store_path).list_rules()[0]
        listing = await second.receive(inbound("d", "/permissions"))
        assert f"{permission.id}: vault file note.md" in listing.replies[0]
        await second.receive(inbound("e", f"/forget-permission {permission.id}"))
        await second.close()

        third = runtime(update(clone, "ccc", "ddd"))
        result = await third.receive(inbound("f", "Edit the note"))
        assert result.disposition == "approval_required"
        await third.receive(inbound("g", "9"))
        assert outputs[-1]["status"] == "rejected"
        assert TomlPermissionStore(store_path).list_rules() == ()
        assert (clone / "note.md").read_bytes() == b"ccc\n"

    asyncio.run(scenario())
    assert any(
        event == "vault_edit_outcome" and payload["commit"] for event, payload in events
    )


@pytest.mark.parametrize("choice", ["1", "9", "/cancel"])
def test_other_choices_never_save_permission(vault, tmp_path, choice):
    from .test_runtime import FakeRunner

    clone, _, original = vault
    store = TomlPermissionStore(tmp_path / "jarvis.toml")
    tool = VaultTools(clone, original._settings, permission_store=store)

    async def scenario():
        proposal = await tool.execute("edit_vault", update(clone, "aaa", "bbb"))
        runtime = PersonalRuntime(
            request_runner=FakeRunner(proposal), permission_store=store
        )
        await runtime.receive(inbound("a", "edit"))
        await runtime.receive(inbound("b", choice))
        assert store.list_rules() == ()

    asyncio.run(scenario())


def test_permission_scope_does_not_cover_other_files_repositories_or_commands(
    vault, tmp_path
):
    clone, remote, original = vault
    store = TomlPermissionStore(tmp_path / "jarvis.toml")
    tool = VaultTools(clone, original._settings, permission_store=store)
    proposal = asyncio.run(tool.execute("edit_vault", update(clone, "aaa", "bbb")))
    rule = store.add(proposal.action.host, proposal.action.prefix)
    assert not rule.matches("ubuntu", "note.md")
    assert not rule.matches(rule.host, "note.md extra")
    assert not rule.matches(rule.host, "Note.md")
    for path in ["other.md", "note.md extra.md", "Note.md"]:
        # Case-insensitive filesystems already contain Note.md; either operation
        # must still require approval under the distinct canonical spelling.
        arguments = (
            update(clone, "aaa", "bbb", path)
            if (clone / path).exists()
            else edit(clone, {"operation": "create", "path": path, "content": "new"})
        )
        step = asyncio.run(tool.execute("edit_vault", arguments))
        assert isinstance(step, ApprovalRequired)
    other_clone = tmp_path / "other-clone"
    git(tmp_path, "-c", "core.autocrlf=false", "clone", str(remote), str(other_clone))
    other = VaultTools(other_clone, original._settings, permission_store=store)
    assert isinstance(
        asyncio.run(other.execute("edit_vault", update(other_clone, "aaa", "bbb"))),
        ApprovalRequired,
    )
    changed_branch = VaultTools(
        clone, replace(original._settings, branch="other"), permission_store=store
    )
    changed_remote = VaultTools(
        clone,
        replace(original._settings, remote=str(tmp_path / "other.git")),
        permission_store=store,
    )
    assert changed_branch._permission_host != tool._permission_host
    assert changed_remote._permission_host != tool._permission_host
    # Mixed batches cannot silently approve an unpermitted second file.
    batch = update(clone, "aaa", "bbb")
    batch["changes"].append({"operation": "create", "path": "new.md", "content": "new"})
    step = asyncio.run(tool.execute("edit_vault", batch))
    assert isinstance(step, ApprovalRequired) and not step.action.allow_save_permission
    assert not (clone / "new.md").exists()


def test_saved_permission_still_requires_valid_base_clean_tree_and_exact_text(
    vault, tmp_path
):
    clone, _, original = vault
    tool = VaultTools(
        clone,
        original._settings,
        permission_store=TomlPermissionStore(tmp_path / "jarvis.toml"),
    )
    proposal = asyncio.run(tool.execute("edit_vault", update(clone, "aaa", "bbb")))
    tool._permission_store.add(proposal.action.host, proposal.action.prefix)
    args = update(clone, "missing", "bbb")
    with pytest.raises(VaultToolError, match="exactly once"):
        asyncio.run(tool.execute("edit_vault", args))
    args = update(clone, "aaa", "bbb")
    args["base_revision"] = "0" * 40
    with pytest.raises(VaultToolError, match="base_revision"):
        asyncio.run(tool.execute("edit_vault", args))
    (clone / "note.md").write_bytes(b"dirty\n")
    with pytest.raises(VaultToolError, match="local changes"):
        asyncio.run(tool.execute("edit_vault", update(clone, "dirty", "bbb")))


def test_permission_save_failure_keeps_proposal_pending(vault, tmp_path, monkeypatch):
    from .test_runtime import FakeRunner

    clone, _, original = vault
    store = TomlPermissionStore(tmp_path / "jarvis.toml")
    tool = VaultTools(clone, original._settings, permission_store=store)
    proposal = asyncio.run(tool.execute("edit_vault", update(clone, "aaa", "bbb")))
    runner = FakeRunner(proposal)
    runtime = PersonalRuntime(request_runner=runner, permission_store=store)

    def fail(*args):
        raise PermissionStoreError("cannot persist")

    monkeypatch.setattr(store, "add", fail)

    async def scenario():
        await runtime.receive(inbound("a", "edit"))
        result = await runtime.receive(inbound("b", "2"))
        assert result.disposition == "permission_save_failed"
        assert runner.resumes == []
        assert (clone / "note.md").read_bytes() == b"aaa\n"
        await runtime.receive(inbound("c", "1"))
        assert len(runner.resumes) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("aaa\n", "aaa\nAdded a reminder.\n", "Add:\nAdded a reminder."),
        ("aaa\n", "", "Remove:\naaa"),
        ("aaa\n", "aaa\n\n", "Add:\n(blank line)"),
    ],
)
def test_plain_preview_handles_additions_removals_and_blank_lines(
    vault, old, new, expected
):
    clone, _, tool = vault
    step = asyncio.run(tool.execute("edit_vault", update(clone, old, new)))
    assert isinstance(step, ApprovalRequired)
    assert expected in step.action.display
    assert "Unified diff" not in step.action.display
    assert "Commit message" not in step.action.display
    assert (clone / "note.md").read_bytes() == b"aaa\n"


def test_append_to_note_without_final_newline_is_an_addition():
    from jarvis_personal_runtime.vault_git import _PreparedChange

    tool = object.__new__(VaultTools)
    change = _PreparedChange(
        "update", "note.md", b"Original.", b"Original.\nAdded.", ""
    )
    preview = tool._approval_display([change])
    assert "Add:\nAdded." in preview
    assert "Replace:" not in preview
