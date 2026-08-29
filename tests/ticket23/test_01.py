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


def test_clean_vault_syncs_before_bounded_path_text_tag_frontmatter_and_link_reads(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    synchronizer = ControlledVaultSynchronizer(
        last_synchronized_at=NOW - timedelta(hours=1)
    )
    connector = _connector(tmp_path, synchronizer)

    by_path = connector.read(VaultReadInput(query="Projects/Alpha"))
    by_tag = connector.read(VaultReadInput(query="work"))
    by_frontmatter = connector.read(VaultReadInput(query="Sam"))
    by_link = connector.read(VaultReadInput(query="Roadmap"))
    by_link_label = connector.read(VaultReadInput(query="the guide"))
    exact_title = connector.read(VaultReadInput(title="Alpha Plan"))

    assert synchronizer.calls == 6
    assert by_path.synchronized_at == NOW
    assert by_path.stale_warning is None
    assert [excerpt.path for excerpt in by_path.excerpts] == ["Projects/Alpha.md"]
    assert [excerpt.path for excerpt in by_tag.excerpts] == ["Projects/Alpha.md"]
    assert [excerpt.path for excerpt in by_frontmatter.excerpts] == [
        "Projects/Alpha.md"
    ]
    assert {excerpt.path for excerpt in by_link.excerpts} == {
        "Projects/Alpha.md",
        "Roadmap.md",
    }
    assert [excerpt.path for excerpt in by_link_label.excerpts] == [
        "Projects/Alpha.md",
        "Guides/How.md",
    ]
    assert [excerpt.path for excerpt in exact_title.excerpts] == ["Projects/Alpha.md"]
    assert all(
        excerpt.start_line >= 1 and excerpt.end_line >= excerpt.start_line
        for excerpt in by_link.excerpts
    )
    assert all(len(excerpt.text) <= 600 for excerpt in by_link.excerpts)


def test_exact_path_read_preserves_complete_small_note_for_exact_write_context(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    content = "one\ntwo\nthree\nfour\nfive\n"
    exact_path = tmp_path / "Projects" / "Exact.md"
    exact_path.write_text(content, encoding="utf-8", newline="")
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )

    result = connector.read(VaultReadInput(path="Projects/Exact.md"))

    assert len(result.excerpts) == 1
    excerpt = result.excerpts[0]
    assert excerpt.start_line == 1
    assert excerpt.end_line == 5
    assert excerpt.text == content
    assert excerpt.complete is True
    assert excerpt.ends_with_newline is True


def test_exact_path_read_preserves_complete_bounded_note_for_exact_write_context(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    content = "# Acceptance\n\n" + ("bounded vault content\n" * 40)
    exact_path = tmp_path / "Projects" / "Acceptance.md"
    exact_path.write_text(content, encoding="utf-8", newline="")
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )

    result = connector.read(VaultReadInput(path="Projects/Acceptance.md"))

    assert len(result.excerpts) == 1
    excerpt = result.excerpts[0]
    assert len(content) > 600
    assert excerpt.text == content
    assert excerpt.complete is True
    assert excerpt.ends_with_newline is True


def test_unavailable_sync_permits_only_a_clean_stale_read_with_age_disclosure(
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

    stale = connector.read(VaultReadInput(query="Alpha"))

    assert stale.synchronized_at == last_sync
    assert (
        stale.stale_warning
        == "Knowledge-vault synchronization is unavailable; results may be stale (age: 2h 5m)."
    )
    assert [excerpt.path for excerpt in stale.excerpts] == [
        "Projects/Alpha.md",
        "Roadmap.md",
    ]

    dirty = _connector(
        tmp_path,
        ControlledVaultSynchronizer(
            last_synchronized_at=last_sync,
            failure="remote unavailable",
            clean=False,
        ),
    )
    with pytest.raises(VaultReadError, match="clean synchronized clone"):
        dirty.read(VaultReadInput(query="Alpha"))


def test_non_fast_forward_state_never_falls_back_to_a_stale_read(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    connector = _connector(
        tmp_path,
        ControlledVaultSynchronizer(
            last_synchronized_at=NOW - timedelta(hours=2),
            failure=VaultRepositoryConflict("fast-forward merge failed"),
        ),
    )

    with pytest.raises(VaultReadError, match="explicit recovery"):
        connector.read(VaultReadInput(query="Alpha"))


@pytest.mark.parametrize(
    "read_request",
    (
        VaultReadInput(path="../outside.md"),
        VaultReadInput(path="Projects/../Roadmap.md"),
        VaultReadInput(path=".obsidian/private.md"),
        VaultReadInput(path="attachments/private.md"),
        VaultReadInput(path="Projects/Alpha.txt"),
    ),
)
def test_vault_read_rejects_traversal_hidden_and_excluded_paths(
    tmp_path: Path, read_request: VaultReadInput
) -> None:
    _vault(tmp_path)
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )

    with pytest.raises(VaultReadError, match="not an ordinary knowledge-vault note"):
        connector.read(read_request)

    expected_codes = {
        "../outside.md": "outside_root",
        "Projects/../Roadmap.md": "outside_root",
        ".obsidian/private.md": "excluded_path",
        "attachments/private.md": "excluded_path",
        "Projects/Alpha.txt": "unsupported_file_type",
    }
    assert read_request.path is not None
    with pytest.raises(VaultReadError) as error:
        connector.read(read_request)
    assert error.value.code == expected_codes[read_request.path]


def test_vault_read_rejects_absolute_and_noncanonical_note_selectors(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )

    for value in ("/Roadmap.md", "C:/Roadmap.md", "Projects//Alpha.md"):
        with pytest.raises(
            VaultReadError, match="not an ordinary knowledge-vault note"
        ):
            connector.read(VaultReadInput(path=value))


def test_vault_read_rejects_a_note_symlink_even_when_its_target_is_markdown(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("outside vault", encoding="utf-8")
    link = tmp_path / "Projects" / "linked.md"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("the test host does not permit creating symlinks")
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )

    with pytest.raises(VaultReadError, match="not an ordinary knowledge-vault note"):
        connector.read(VaultReadInput(path="Projects/linked.md"))


@pytest.mark.parametrize(
    "path",
    ("Projects/attachments/Private.md", "nested-module/Private.md"),
)
def test_vault_read_rejects_nested_excluded_directories_and_submodules(
    tmp_path: Path, path: str
) -> None:
    _vault(tmp_path)
    (tmp_path / "Projects" / "attachments").mkdir()
    (tmp_path / "Projects" / "attachments" / "Private.md").write_text(
        "private", encoding="utf-8"
    )
    (tmp_path / "nested-module").mkdir()
    (tmp_path / "nested-module" / ".git").write_text(
        "gitdir: ../.git/modules/nested-module", encoding="utf-8"
    )
    (tmp_path / "nested-module" / "Private.md").write_text("private", encoding="utf-8")
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )

    with pytest.raises(VaultReadError, match="not an ordinary knowledge-vault note"):
        connector.read(VaultReadInput(path=path))


def test_exact_title_ignores_title_lines_outside_leading_frontmatter(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    (tmp_path / "BodyTitle.md").write_text(
        "A body paragraph.\n\n"
        "title: This is not frontmatter\n\n"
        "```yaml\n"
        "title: This is also not frontmatter\n"
        "```\n\n"
        "# Actual heading\n",
        encoding="utf-8",
    )
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )

    result = connector.read(VaultReadInput(title="Actual heading"))

    assert [excerpt.path for excerpt in result.excerpts] == ["BodyTitle.md"]


def test_vault_read_rejects_oversized_notes_and_total_search_bytes(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    (tmp_path / "TooLarge.md").write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    connector = _connector(
        tmp_path, ControlledVaultSynchronizer(last_synchronized_at=NOW)
    )

    with pytest.raises(VaultReadError, match="per-note byte limit"):
        connector.read(VaultReadInput(path="TooLarge.md"))

    (tmp_path / "TooLarge.md").unlink()
    for index in range(9):
        (tmp_path / f"Large-{index}.md").write_text(
            "scan budget\n" + "x" * (64 * 1024 - 1_024), encoding="utf-8"
        )

    with pytest.raises(VaultReadError, match="total byte limit"):
        connector.read(VaultReadInput(query="scan budget"))

    for path in tmp_path.glob("Large-*.md"):
        path.unlink()
    for index in range(129):
        (tmp_path / f"Note-{index}.md").write_text("small note", encoding="utf-8")

    with pytest.raises(VaultReadError, match="note-inspection limit"):
        connector.read(VaultReadInput(query="small"))


def test_production_synchronizer_enforces_dedicated_ssh_config_and_host_verification(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def run_process(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    synchronizer = SubprocessVaultSynchronizer(
        git_executable=PurePosixPath("/usr/bin/git"),
        ssh_executable=PurePosixPath("/usr/bin/ssh"),
        ssh_config_path=PurePosixPath("/etc/jarvis/vault-ssh-config"),
        known_hosts_path=PurePosixPath("/etc/jarvis/vault-known-hosts"),
        synchronization_state=InMemoryDurableStateStore(),
        run_process=run_process,
    )

    synchronized_at = synchronizer.synchronize(tmp_path, now=NOW)

    assert synchronized_at == NOW
    assert [call["args"][0][-1] for call in calls] == [
        "--porcelain",
        "origin",
        "FETCH_HEAD",
    ]
    environment = calls[0]["kwargs"]["env"]
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_SSH_COMMAND"] == (
        "/usr/bin/ssh -F /etc/jarvis/vault-ssh-config -o BatchMode=yes "
        "-o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "
        "-o UserKnownHostsFile=/etc/jarvis/vault-known-hosts "
        "-o GlobalKnownHostsFile=/dev/null"
    )


def test_production_synchronizer_translates_fetch_timeouts_to_remote_unavailable(
    tmp_path: Path,
) -> None:
    def run_process(*args: object, **_kwargs: object) -> SimpleNamespace:
        command = args[0]
        if command[-1] == "origin":
            raise TimeoutExpired(command, 15)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    synchronizer = SubprocessVaultSynchronizer(
        git_executable=PurePosixPath("/usr/bin/git"),
        ssh_executable=PurePosixPath("/usr/bin/ssh"),
        ssh_config_path=PurePosixPath("/etc/jarvis/vault-ssh-config"),
        known_hosts_path=PurePosixPath("/etc/jarvis/vault-known-hosts"),
        synchronization_state=InMemoryDurableStateStore(),
        run_process=run_process,
    )

    with pytest.raises(VaultRemoteUnavailable, match="timed out"):
        synchronizer.synchronize(tmp_path, now=NOW)


def test_vault_tool_enforces_one_deadline_when_process_runner_is_slow(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    invocation_durations: list[float] = []

    def slow_process(*_args: object, **_kwargs: object) -> SimpleNamespace:
        time.sleep(0.15)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    synchronizer = SubprocessVaultSynchronizer(
        git_executable=PurePosixPath("/usr/bin/git"),
        ssh_executable=PurePosixPath("/usr/bin/ssh"),
        ssh_config_path=PurePosixPath("/etc/jarvis/vault-ssh-config"),
        known_hosts_path=PurePosixPath("/etc/jarvis/vault-known-hosts"),
        synchronization_state=InMemoryDurableStateStore(),
        run_process=slow_process,
    )
    connector = KnowledgeVaultConnector(
        root=tmp_path,
        synchronizer=synchronizer,
        now=lambda: NOW,
        read_timeout_seconds=0.05,
    )

    def run_sync(agent: object, _text: str, **_kwargs: object) -> object:
        loop = asyncio.new_event_loop()
        started = time.monotonic()
        try:
            loop.run_until_complete(
                agent.tools[1].on_invoke_tool(None, json.dumps({"query": "Alpha"}))
            )
        finally:
            loop.close()
        invocation_durations.append(time.monotonic() - started)
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

    assert result.outcome == "unavailable"
    assert "knowledge vault" in result.reply_text
    assert "service timed out" in result.reply_text
    assert invocation_durations[0] < 0.12
