# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
"""Ticket 23 contract tests for the bounded knowledge-vault read connector."""

from __future__ import annotations

import asyncio

import json

import os

import time

from datetime import UTC, datetime, timedelta

from pathlib import Path, PurePosixPath

from subprocess import TimeoutExpired

from types import SimpleNamespace

import pytest

from jarvis_control_plane.adapters import (
    InMemoryDurableStateStore,
    SQLiteDurableStateStore,
)

from jarvis_control_plane.knowledge_vault import (
    ControlledVaultSynchronizer,
    KnowledgeVaultConnector,
    SubprocessVaultSynchronizer,
    VaultReadError,
    VaultReadInput,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
)

from jarvis_control_plane.models import OrchestrationRequest, RequestState

from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
)

from jarvis_control_plane.proposal_translation import build_instructions

NOW = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)


def _request() -> OrchestrationRequest:
    return OrchestrationRequest(
        state=RequestState(
            request_id="request-ticket23",
            event_id="event-ticket23",
            message_id="message-ticket23",
            operator_id="operator.test",
            session_id="working-session-ticket23",
            chat_id="operator.test",
            created_at=NOW,
            updated_at=NOW,
            status="accepted",
            phase="orchestration",
            model="gpt-5.6-terra",
            reasoning="high",
        ),
        text="Search the knowledge vault for the Alpha plan.",
    )


def _vault(root: Path) -> None:
    (root / "Projects").mkdir(parents=True)
    (root / "Guides").mkdir()
    (root / ".obsidian").mkdir()
    (root / "attachments").mkdir()
    (root / "Projects" / "Alpha.md").write_text(
        "---\n"
        "title: Alpha Plan\n"
        "owner: Sam\n"
        "tags:\n"
        "  - work\n"
        "  - priority\n"
        "---\n\n"
        "# Alpha plan\n\n"
        "Use [[Roadmap]] and [the guide](../Guides/How.md).\n",
        encoding="utf-8",
    )
    (root / "Roadmap.md").write_text(
        "# Roadmap\n\nThe Alpha milestone is scheduled.\n", encoding="utf-8"
    )
    (root / "Guides" / "How.md").write_text(
        "# How\n\nGuide content.\n", encoding="utf-8"
    )
    (root / ".obsidian" / "private.md").write_text(
        "should never appear", encoding="utf-8"
    )
    (root / "attachments" / "private.md").write_text(
        "should never appear", encoding="utf-8"
    )


def _connector(
    root: Path, synchronizer: ControlledVaultSynchronizer
) -> KnowledgeVaultConnector:
    return KnowledgeVaultConnector(
        root=root,
        synchronizer=synchronizer,
        now=lambda: NOW,
    )


def test_vault_deadline_is_passed_to_the_process_runner(tmp_path: Path) -> None:
    observed_timeouts: list[float] = []

    def timed_out_process(*args: object, **kwargs: object) -> SimpleNamespace:
        observed_timeouts.append(float(kwargs["timeout"]))
        raise TimeoutExpired(args[0], kwargs["timeout"])

    synchronizer = SubprocessVaultSynchronizer(
        git_executable=PurePosixPath("/usr/bin/git"),
        ssh_executable=PurePosixPath("/usr/bin/ssh"),
        ssh_config_path=PurePosixPath("/etc/jarvis/vault-ssh-config"),
        known_hosts_path=PurePosixPath("/etc/jarvis/vault-known-hosts"),
        synchronization_state=InMemoryDurableStateStore(),
        run_process=timed_out_process,
    )

    with pytest.raises(VaultRepositoryConflict, match="timed out"):
        synchronizer.is_clean(tmp_path, deadline=time.monotonic() + 0.05)

    assert observed_timeouts and observed_timeouts[0] <= 0.05


def test_production_synchronizer_restores_last_successful_sync_from_durable_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    state = SQLiteDurableStateStore(database)

    def run_process(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    arguments = {
        "git_executable": PurePosixPath("/usr/bin/git"),
        "ssh_executable": PurePosixPath("/usr/bin/ssh"),
        "ssh_config_path": PurePosixPath("/etc/jarvis/vault-ssh-config"),
        "known_hosts_path": PurePosixPath("/etc/jarvis/vault-known-hosts"),
        "synchronization_state": state,
        "run_process": run_process,
    }
    try:
        SubprocessVaultSynchronizer(**arguments).synchronize(tmp_path, now=NOW)
    finally:
        state.close()
    restored_state = SQLiteDurableStateStore(database)
    try:
        restored = SubprocessVaultSynchronizer(
            **{**arguments, "synchronization_state": restored_state}
        )

        assert restored.last_synchronized_at == NOW
    finally:
        restored_state.close()


def test_orchestration_exposes_the_vault_only_as_a_closed_bounded_read_tool(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )
    captured: dict[str, object] = {}

    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        tools = _agent.tools
        assert [tool.name for tool in tools] == [
            "read_request_context",
            "read_knowledge_vault",
        ]
        vault_tool = tools[1]
        captured["vault_result"] = asyncio.run(
            vault_tool.on_invoke_tool(None, json.dumps({"query": "Alpha"}))
        )
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="The vault search is complete.",
                proposal=None,
            )
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        reasoning_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_config_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        vault_read_tool=connector.as_bounded_read_tool(),
    )

    result = adapter.run(_request())

    assert captured["vault_result"]["source"] == "knowledge_vault"
    assert captured["vault_result"]["stale_warning"] is None
    assert [entry["path"] for entry in captured["vault_result"]["excerpts"]] == [
        "Projects/Alpha.md",
        "Roadmap.md",
    ]
    assert [milestone.stage for milestone in result.milestones] == [
        "orchestration_started",
        "bounded_read",
    ]


def test_orchestration_refuses_a_non_markdown_vault_path_without_a_proposal(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )

    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        result = asyncio.run(
            _agent.tools[1].on_invoke_tool(
                None, json.dumps({"path": "Projects/Alpha.txt"})
            )
        )
        assert result["unavailable"] is True
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I found the requested file.",
                proposal=None,
            )
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        reasoning_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_config_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        vault_read_tool=connector.as_bounded_read_tool(),
    )

    result = adapter.run(_request())

    assert result.outcome == "unavailable"
    assert "ordinary Markdown note" in result.reply_text
    assert result.proposal is None
    assert result.proposal_intent is None


def test_vault_write_instructions_require_a_fresh_exact_path_read() -> None:
    instructions = build_instructions(
        has_vault_read=True,
        has_vault_write=True,
    )

    assert "invoke read_knowledge_vault for each exact target path" in instructions
    assert "require its complete and ends_with_newline metadata" in instructions
    assert "never reconstruct content from conversation history" in instructions
    assert "If an exact-path read is not marked complete" in instructions
    assert '{"changes": {"Notes/example.md": "<complete content>"}}' in instructions
    assert "do not wrap path/content in another object" in instructions


def test_orchestration_deterministically_discloses_a_stale_vault_read(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    last_sync = NOW - timedelta(hours=2, minutes=5)
    connector = _connector(
        tmp_path,
        ControlledVaultSynchronizer(
            last_synchronized_at=last_sync,
            failure="remote unavailable",
        ),
    )

    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        asyncio.run(
            _agent.tools[1].on_invoke_tool(None, json.dumps({"query": "Alpha"}))
        )
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="The vault search is complete.",
            )
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        reasoning_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_config_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        vault_read_tool=connector.as_bounded_read_tool(),
    )

    result = adapter.run(_request())

    assert "Knowledge-vault synchronization is unavailable" in result.reply_text
    assert (
        "Last successful synchronization: 2026-08-06T07:55:00+00:00"
        in result.reply_text
    )


def test_orchestration_rejects_a_vault_tool_outside_its_closed_contract(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )
    tool = connector.as_bounded_read_tool()
    object.__setattr__(tool, "name", "execute_arbitrary_path")

    with pytest.raises(ValueError, match="outside the closed tool set"):
        AgentsSdkOrchestrationAdapter(
            vault_read_tool=tool,
        )
