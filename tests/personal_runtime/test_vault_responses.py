"""Exercise the vault approval continuation through the real Responses runner."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from jarvis_personal_runtime.responses import (
    DirectResponsesRunner,
    PreparedToolCollection,
    ResponsesResult,
)
from jarvis_personal_runtime.runtime import ApprovalDecision, ApprovalRequired
from jarvis_personal_runtime.vault_git import VaultGitSettings, VaultTools


def test_responses_approval_commits_exactly_once_and_returns_sync_result(
    tmp_path: Path,
) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    remote = tmp_path / "remote.git"
    clone = tmp_path / "vault"
    git("init", "--bare", "--initial-branch=main", str(remote))
    git("clone", str(remote), str(clone))
    (clone / "note.md").write_bytes(b"# Note\n\nBefore.\n")
    git("-C", str(clone), "add", "note.md")
    git(
        "-C",
        str(clone),
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.com",
        "commit",
        "-m",
        "Initial",
    )
    git("-C", str(clone), "push", "origin", "main")
    base = git("-C", str(clone), "rev-parse", "HEAD")
    arguments = {
        "base_revision": base,
        "commit_message": "jarvis: update note",
        "changes": [
            {
                "operation": "update",
                "path": "note.md",
                "replacements": [{"old_text": "Before.", "new_text": "After."}],
            }
        ],
    }

    class Provider:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def create(
            self, request: dict[str, object], *, timeout: float
        ) -> ResponsesResult:
            self.calls.append(request)
            if len(self.calls) == 1:
                return ResponsesResult(
                    output=(
                        {
                            "type": "function_call",
                            "call_id": "vault-edit",
                            "name": "edit_vault",
                            "arguments": json.dumps(arguments),
                        },
                    ),
                    output_text="",
                )
            return ResponsesResult(output=(), output_text="Note committed and synced.")

    provider = Provider()
    tool = VaultTools(
        clone,
        VaultGitSettings(
            remote=str(remote),
            branch="main",
            note_directories=(".",),
        ),
    )
    runner = DirectResponsesRunner(
        provider,
        tools=PreparedToolCollection(tool),
        request_timeout_seconds=30,
    )

    async def scenario() -> None:
        step = await runner.run(
            "Update the note",
            model="gpt-6-luna",
            reasoning="medium",
            system_prompt="Use the vault tool.",
        )
        assert isinstance(step, ApprovalRequired)
        assert not step.action.allow_save_permission
        assert "Before." in step.action.display and "After." in step.action.display
        assert (clone / "note.md").read_bytes() == b"# Note\n\nBefore.\n"
        assert len(provider.calls) == 1
        completed = await runner.resume(
            ApprovalDecision.APPROVE_ONCE, step.continuation
        )
        assert completed.reply == "Note committed and synced."

    asyncio.run(scenario())
    assert (clone / "note.md").read_bytes() == b"# Note\n\nAfter.\n"
    head = git("-C", str(clone), "rev-parse", "HEAD")
    assert git("--git-dir", str(remote), "rev-parse", "main") == head
    assert git("-C", str(clone), "rev-list", "--count", f"{base}..HEAD") == "1"
    transcript = provider.calls[-1]["input"]
    assert isinstance(transcript, list)
    output = json.loads(transcript[-1]["output"])
    assert output["status"] == "synced"
    assert output["approval"] == {
        "decision": "approved_once",
        "preview_shown": True,
        "source": "operator_reply",
    }
