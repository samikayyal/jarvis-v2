from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis_personal_runtime.responses import DirectResponsesRunner, ResponsesResult
from jarvis_personal_runtime.vault import ReadVaultTool, VaultToolError


def _execute(tool: ReadVaultTool, **arguments: object) -> dict[str, object]:
    import asyncio

    return json.loads(asyncio.run(tool.execute("read_vault", arguments)))


def test_tool_exposes_one_small_strict_responses_schema(tmp_path: Path) -> None:
    tool = ReadVaultTool(tmp_path)

    assert tool.definitions == (
        {
            "type": "function",
            "name": "read_vault",
            "description": (
                "Search the configured Markdown vault or read one exact Markdown "
                "file. Use a vault-relative POSIX path for read mode."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["search", "read"]},
                    "value": {"type": "string", "minLength": 1, "maxLength": 512},
                },
                "required": ["mode", "value"],
                "additionalProperties": False,
            },
        },
    )


def test_direct_responses_loop_executes_the_prepared_vault_contract(
    tmp_path: Path,
) -> None:
    class Responses:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        async def create(
            self, request: dict[str, object], *, timeout: float
        ) -> ResponsesResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ResponsesResult(
                    output=(
                        {
                            "type": "function_call",
                            "call_id": "call_vault",
                            "name": "read_vault",
                            "arguments": '{"mode":"read","value":"note.md"}',
                        },
                    ),
                    output_text="",
                )
            return ResponsesResult(output=(), output_text="Found it.")

    (tmp_path / "note.md").write_bytes(b"vault content")
    responses = Responses()
    runner = DirectResponsesRunner(
        responses,
        tools=ReadVaultTool(tmp_path),
        request_timeout_seconds=30,
    )

    import asyncio

    result = asyncio.run(
        runner.run(
            "Read the note",
            model="gpt-5.6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )
    )

    assert result.reply == "Found it."
    assert responses.requests[0]["tools"] == list(ReadVaultTool.definitions)
    continuation = responses.requests[1]["input"]
    assert isinstance(continuation, list)
    assert json.loads(continuation[-1]["output"]) == {
        "mode": "read",
        "path": "note.md",
        "content": "vault content",
    }


def test_read_returns_one_exact_utf8_markdown_file(tmp_path: Path) -> None:
    note = tmp_path / "Projects" / "Jarvis.md"
    note.parent.mkdir()
    note.write_bytes(b"# Jarvis\n\nPrivate notes.\n")
    tool = ReadVaultTool(tmp_path)

    result = _execute(tool, mode="read", value="Projects/Jarvis.md")

    assert result == {
        "mode": "read",
        "path": "Projects/Jarvis.md",
        "content": "# Jarvis\n\nPrivate notes.\n",
    }


@pytest.mark.parametrize(
    "value",
    [
        "../outside.md",
        "Projects/../outside.md",
        "/etc/passwd.md",
        "C:/vault/note.md",
        "Projects\\Jarvis.md",
        "Projects/todo.txt",
    ],
)
def test_read_rejects_traversal_absolute_and_non_markdown_targets(
    tmp_path: Path, value: str
) -> None:
    tool = ReadVaultTool(tmp_path)

    with pytest.raises(VaultToolError):
        _execute(tool, mode="read", value=value)


def test_read_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(VaultToolError):
        _execute(ReadVaultTool(tmp_path), mode="read", value="linked.md")


def test_search_is_case_insensitive_deterministic_and_bounded(tmp_path: Path) -> None:
    for index in range(10):
        note = tmp_path / f"note-{index:02}.md"
        note.write_text(
            f"# Note {index}\nline before\nNeedle result {index}\nline after\n",
            encoding="utf-8",
        )
    (tmp_path / "ignored.txt").write_text("needle", encoding="utf-8")
    tool = ReadVaultTool(tmp_path)

    result = _execute(tool, mode="search", value="NEEDLE")

    assert result["mode"] == "search"
    assert result["query"] == "NEEDLE"
    matches = result["matches"]
    assert isinstance(matches, list)
    assert [match["path"] for match in matches] == [
        f"note-{index:02}.md" for index in range(8)
    ]
    assert matches[0]["excerpt"] == "line before\nNeedle result 0\nline after"


def test_tool_rejects_writes_unknown_fields_and_oversized_results(
    tmp_path: Path,
) -> None:
    note = tmp_path / "note.md"
    note.write_text("content that is too long", encoding="utf-8")
    tool = ReadVaultTool(tmp_path, max_result_chars=20)

    with pytest.raises(VaultToolError, match="mode"):
        _execute(tool, mode="write", value="note.md")
    with pytest.raises(VaultToolError, match="arguments"):
        _execute(tool, mode="read", value="note.md", content="replacement")
    with pytest.raises(VaultToolError, match="result"):
        _execute(tool, mode="read", value="note.md")

    assert note.read_text(encoding="utf-8") == "content that is too long"


def test_read_and_search_enforce_filesystem_scan_bounds(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.md"
    oversized.write_bytes(b"x" * (64 * 1024 + 1))
    tool = ReadVaultTool(tmp_path)

    with pytest.raises(VaultToolError, match="byte limit"):
        _execute(tool, mode="read", value="oversized.md")

    oversized.unlink()
    for index in range(129):
        (tmp_path / f"note-{index:03}.md").write_text("no match", encoding="utf-8")
    with pytest.raises(VaultToolError, match="note limit"):
        _execute(tool, mode="search", value="absent")


def test_tool_rejects_unknown_name_and_oversized_search_query(tmp_path: Path) -> None:
    tool = ReadVaultTool(tmp_path)

    with pytest.raises(VaultToolError, match="unknown prepared tool"):
        import asyncio

        asyncio.run(tool.execute("run_terminal", {"mode": "search", "value": "x"}))
    with pytest.raises(VaultToolError, match="query"):
        _execute(tool, mode="search", value="x" * 201)
    with pytest.raises(VaultToolError, match="query"):
        _execute(tool, mode="search", value="   ")
