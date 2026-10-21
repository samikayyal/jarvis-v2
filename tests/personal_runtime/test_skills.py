from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jarvis_personal_runtime.config import RuntimeConfig
from jarvis_personal_runtime.google import _definitions as google_definitions
from jarvis_personal_runtime.permissions import TomlPermissionStore
from jarvis_personal_runtime.reminders import ReminderTools
from jarvis_personal_runtime.responses import (
    DirectResponsesRunner,
    OpenAIRawResponsesAdapter,
    PreparedToolCollection,
    ResponsesResult,
    build_direct_responses_runner,
    local_input_tokens,
)
from jarvis_personal_runtime.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    ContextLimitReached,
    PendingAction,
)
from jarvis_personal_runtime.skill_tools import SkillTools
from jarvis_personal_runtime.terminal import CommandResult, RunTerminalTool
from jarvis_personal_runtime.vault import ReadVaultTool
from jarvis_personal_runtime.vault_git import VaultTools


class Tools:
    def __init__(self, definitions=VaultTools.definitions, *, approval=False):
        self.definitions = definitions
        self.calls = []
        self.resumptions = []
        self.continuation = object()
        self.approval = approval

    async def execute(self, name, arguments):
        self.calls.append((name, arguments))
        if self.approval:
            return ApprovalRequired(
                PendingAction(host="ubuntu", prefix="edit", display="Approve?"),
                self.continuation,
            )
        return '{"ok":true}'

    async def resume(self, continuation, *, approved):
        assert continuation is self.continuation
        self.resumptions.append(approved)
        return json.dumps({"approved": approved})


class Responses:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    async def create(self, request, *, timeout):
        self.requests.append(request)
        return self.results.pop(0)


def call(name, arguments, call_id="call_1"):
    return ResponsesResult(
        output=(
            {
                "type": "function_call",
                "name": name,
                "call_id": call_id,
                "arguments": json.dumps(arguments),
            },
        ),
        output_text="",
    )


def run(runner, text="Find my note"):
    return asyncio.run(
        runner.run(text, model="gpt-test", reasoning="low", system_prompt="Help.")
    )


def names(definitions):
    return {item["name"] for item in definitions}


@pytest.mark.parametrize(
    "skill,definitions",
    [
        ("obsidian", VaultTools.definitions),
        ("obsidian", ReadVaultTool.definitions),
        ("google", google_definitions()),
        ("reminders", ReminderTools.definitions),
    ],
)
def test_each_group_hides_all_real_schemas_until_loaded(skill, definitions):
    delegate = Tools(definitions)
    tools = SkillTools(delegate)
    assert names(tools.definitions) == {"load_skill"}
    assert tools.definitions[0]["parameters"]["properties"]["name"]["enum"] == [skill]
    for definition in definitions:
        with pytest.raises(ValueError, match="load the"):
            asyncio.run(tools.execute(definition["name"], {}))
    assert delegate.calls == []
    before = tools.instructions
    asyncio.run(tools.execute("load_skill", {"name": skill}))
    assert len(tools.instructions) > len(before)
    assert tools.definitions[1:] == definitions
    asyncio.run(tools.execute(definitions[0]["name"], {"value": "test"}))
    assert delegate.calls == [(definitions[0]["name"], {"value": "test"})]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"name": "../google"},
        {"name": "google"},
        {"name": []},
        {"name": "obsidian", "path": "untrusted.md"},
    ],
)
def test_invalid_or_unconfigured_skills_do_not_activate_anything(arguments):
    tools = SkillTools(Tools())
    before = tools.instructions
    with pytest.raises(ValueError):
        asyncio.run(tools.execute("load_skill", arguments))
    assert names(tools.definitions) == {"load_skill"}
    assert tools.instructions == before


def test_multiple_skills_are_additive_idempotent_and_reset():
    definitions = VaultTools.definitions + google_definitions()
    tools = SkillTools(Tools(definitions))
    initial = tools.instructions
    asyncio.run(tools.execute("load_skill", {"name": "obsidian"}))
    once = tools.instructions
    asyncio.run(tools.execute("load_skill", {"name": "obsidian"}))
    assert tools.instructions == once
    assert names(tools.definitions) == {"load_skill"} | names(VaultTools.definitions)
    asyncio.run(tools.execute("load_skill", {"name": "google"}))
    assert names(tools.definitions) == {"load_skill"} | names(definitions)
    tools.start_session()
    assert tools.instructions == initial
    assert names(tools.definitions) == {"load_skill"}


def test_custom_manifest_tools_remain_available_and_empty_registry_has_no_loader():
    custom = Tools(({"name": "custom_read", "type": "function"},))
    tools = SkillTools(custom)
    assert tools.definitions == custom.definitions
    assert tools.instructions == ""
    asyncio.run(tools.execute("custom_read", {}))
    assert custom.calls == [("custom_read", {})]
    with pytest.raises(ValueError, match="reserved"):
        SkillTools(Tools(({"name": "load_skill"},)))


def test_loop_discovers_tools_reuses_skill_and_counts_effective_context():
    responses = Responses(
        call("load_skill", {"name": "obsidian"}),
        call("read_vault", {"mode": "read", "value": "note.md"}, "call_2"),
        ResponsesResult(output=(), output_text="Found it."),
        ResponsesResult(output=(), output_text="Follow-up."),
        ResponsesResult(output=(), output_text="New session."),
    )
    delegate = Tools(ReadVaultTool.definitions)
    tools = SkillTools(PreparedToolCollection(delegate))
    runner = DirectResponsesRunner(responses, tools=tools, request_timeout_seconds=30)
    assert run(runner).reply == "Found it."
    first, loaded, final = responses.requests
    assert names(first["tools"]) == {"load_skill"}
    assert names(loaded["tools"]) == {"load_skill"} | names(ReadVaultTool.definitions)
    assert loaded["instructions"] == final["instructions"]
    assert len(loaded["instructions"]) > len(first["instructions"])
    assert json.loads(loaded["input"][-1]["output"]) == {"loaded": "obsidian"}
    assert runner.context_tokens(system_prompt="Help.") == local_input_tokens(
        {
            "instructions": final["instructions"],
            "tools": final["tools"],
            "input": final["input"],
        }
    )
    run(runner, "What about that note?")
    assert responses.requests[-1]["instructions"] == loaded["instructions"]
    runner.start_session()
    run(runner, "Hello")
    assert responses.requests[-1]["instructions"] == first["instructions"]
    assert names(responses.requests[-1]["tools"]) == {"load_skill"}
    assert responses.requests[-1]["input"] == [{"role": "user", "content": "Hello"}]


@pytest.mark.parametrize(
    "decision", [ApprovalDecision.APPROVE_ONCE, ApprovalDecision.REJECT]
)
def test_loaded_write_preserves_approval_and_resume(decision):
    delegate = Tools(approval=True)
    tools = SkillTools(PreparedToolCollection(delegate))
    responses = Responses(
        call("load_skill", {"name": "obsidian"}),
        call("edit_vault", {"changes": []}, "write"),
        ResponsesResult(output=(), output_text="Handled."),
    )
    runner = DirectResponsesRunner(responses, tools=tools, request_timeout_seconds=30)
    pending = run(runner, "Edit the note")
    assert isinstance(pending, ApprovalRequired)
    assert delegate.resumptions == []
    loaded_instructions = responses.requests[-1]["instructions"]
    assert (
        asyncio.run(runner.resume(decision, pending.continuation)).reply == "Handled."
    )
    assert delegate.resumptions == [decision is ApprovalDecision.APPROVE_ONCE]
    assert responses.requests[-1]["instructions"] == loaded_instructions


def test_unloaded_call_returns_error_without_executing_then_can_load():
    delegate = Tools()
    responses = Responses(
        call("delete_vault", {}),
        call("load_skill", {"name": "obsidian"}, "load"),
        ResponsesResult(output=(), output_text="Ready."),
    )
    runner = DirectResponsesRunner(
        responses, tools=SkillTools(delegate), request_timeout_seconds=30
    )
    run(runner)
    assert delegate.calls == []
    assert "load the obsidian skill" in responses.requests[1]["input"][-1]["output"]


def test_loaded_instructions_and_schemas_are_subject_to_context_gate():
    responses = Responses(call("load_skill", {"name": "obsidian"}))
    observed = []

    def count(candidate):
        observed.append(candidate)
        return 100 if len(candidate["tools"]) > 1 else 1

    runner = DirectResponsesRunner(
        responses,
        tools=SkillTools(Tools()),
        request_timeout_seconds=30,
        max_context_tokens=100,
        context_counter=count,
    )
    assert isinstance(run(runner), ContextLimitReached)
    assert len(responses.requests) == 1
    assert len(observed[-1]["instructions"]) > len(observed[0]["instructions"])


def test_default_builder_exposes_terminal_without_live_provider(
    tmp_path: Path, monkeypatch
):
    responses = Responses(ResponsesResult(output=(), output_text="Hello."))
    monkeypatch.setattr(
        OpenAIRawResponsesAdapter, "from_api_key", lambda *a, **k: responses
    )
    config = RuntimeConfig(
        root=tmp_path,
        ubuntu_working_directory=tmp_path,
        trace_path=tmp_path / "trace.jsonl",
    )
    runner = build_direct_responses_runner("test-key", config)
    run(runner, "Hello")
    exposed = responses.requests[0]["tools"]
    assert names(exposed) == {"run_terminal"}
    assert exposed[0]["parameters"]["properties"]["host"]["enum"] == ["ubuntu"]


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("mode", ["read", "approve", "reject"])
def test_direct_terminal_preserves_hosts_reset_and_approval(tmp_path, windows, mode):
    class Executor:
        def __init__(self):
            self.calls = []

        async def run(self, command, **kwargs):
            self.calls.append(command)
            return CommandResult(exit_code=0, stdout="checked", stderr="")

    executor = Executor()
    terminal = RunTerminalTool(
        working_directory=tmp_path,
        read_only_prefixes=("hostname",),
        permission_store=TomlPermissionStore(tmp_path / "permissions.toml"),
        executor=executor,
        windows_executor=executor if windows else None,
        windows_working_directory="C:\\Work" if windows else None,
        windows_read_only_prefixes=("hostname",),
        timeout_seconds=10,
        max_output_chars=1000,
    )
    tools = SkillTools(
        PreparedToolCollection(terminal, Tools(ReadVaultTool.definitions))
    )
    initial = tools.definitions
    assert names(initial) == {"load_skill", "run_terminal"}
    assert initial[0]["parameters"]["properties"]["name"]["enum"] == ["obsidian"]
    assert initial[1]["parameters"]["properties"]["host"]["enum"] == (
        ["ubuntu", "windows"] if windows else ["ubuntu"]
    )
    asyncio.run(tools.execute("load_skill", {"name": "obsidian"}))
    tools.start_session()
    assert tools.definitions == initial
    with pytest.raises(ValueError, match="unavailable skill"):
        asyncio.run(tools.execute("load_skill", {"name": "terminal"}))

    command = "hostname" if mode == "read" else "echo proposed-change"
    responses = Responses(
        call(
            "run_terminal",
            {"host": "windows" if windows else "ubuntu", "command": command},
        ),
        ResponsesResult(output=(), output_text="Handled."),
    )
    runner = DirectResponsesRunner(responses, tools=tools, request_timeout_seconds=30)
    result = run(runner, "Check the host")
    if mode != "read":
        assert isinstance(result, ApprovalRequired)
        assert executor.calls == []
        decision = (
            ApprovalDecision.APPROVE_ONCE
            if mode == "approve"
            else ApprovalDecision.REJECT
        )
        result = asyncio.run(runner.resume(decision, result.continuation))
    assert result.reply == "Handled."
    assert executor.calls == ([] if mode == "reject" else [command])
    assert len(responses.requests) == 2
    assert names(responses.requests[0]["tools"]) == {"load_skill", "run_terminal"}
