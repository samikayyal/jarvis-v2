# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
"""Ticket 24 contract tests for exact knowledge-vault writes."""

from __future__ import annotations

import json

import shutil

import subprocess

from datetime import UTC, datetime

from pathlib import Path, PurePosixPath

from types import SimpleNamespace

import pytest

from test_support import build_receiver_components

from jarvis_control_plane import (
    ControlledActionDispatcher,
    ControlledOrchestrationAdapter,
    InboundMessage,
    RoutedActionDispatcher,
    SignedInboundEvent,
    SubprocessVaultSynchronizer,
    VaultPushPreDispatchFailure,
    VaultPushUnknownOutcome,
)

from jarvis_control_plane.knowledge_vault_writes import (
    ControlledVaultWriteRepository,
    KnowledgeVaultWriteConnector,
    VaultWriteChange,
    VaultWriteError,
    VaultWriteProposal,
    VaultWriteRequest,
    render_vault_unified_diff,
)

from jarvis_control_plane.models import OrchestrationRequest, RequestState

from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
    AgentsSdkProposal,
)

from jarvis_control_plane.ports import ActionDispatcherError, OrchestrationAdapterError

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

BASE = "a" * 40


def _orchestration_request() -> OrchestrationRequest:
    return OrchestrationRequest(
        state=RequestState(
            request_id="request-orchestration",
            event_id="event-orchestration",
            message_id="message-orchestration",
            operator_id="operator.test",
            session_id="working-session-24",
            chat_id="operator.test",
            created_at=NOW,
            updated_at=NOW,
            status="accepted",
            phase="orchestration",
            model="gpt-5.6-terra",
            reasoning="high",
        ),
        text="update the Alpha note",
    )


def _connector(
    root: Path,
    *,
    repository: ControlledVaultWriteRepository | None = None,
) -> tuple[KnowledgeVaultWriteConnector, ControlledVaultWriteRepository]:
    (root / "Projects").mkdir(parents=True)
    (root / "Projects" / "Alpha.md").write_text(
        "# Alpha\n\nStatus: draft\n", encoding="utf-8"
    )
    selected_repository = repository or ControlledVaultWriteRepository(
        current_commit=BASE,
        remote_commit=BASE,
    )
    return (
        KnowledgeVaultWriteConnector(
            root=root,
            repository=selected_repository,
            now=lambda: NOW,
            allowed_note_directories=("Projects",),
        ),
        selected_repository,
    )


class _UnrelatedStagedRepository(ControlledVaultWriteRepository):
    """Expose the difference between path-scoped and complete-index checks."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.path_scoped_diff: str | None = None
        self.complete_index_diff: str | None = None
        self.staged_diff_requests: list[tuple[object, ...]] = []

    def staged_diff(
        self,
        _root: Path,
        *paths: object,
        deadline: float | None = None,
    ) -> str | None:
        self.staged_diff_requests.append(paths)
        if paths:
            return self.path_scoped_diff
        return self.complete_index_diff


class _IdentityLifecycle:
    def bind_proposal(self, action):
        return action

    def validate_pending_action(self, action) -> None:
        return


def test_subprocess_renderer_and_staged_verifier_share_git_newline_semantics(
    tmp_path: Path,
) -> None:
    git_executable = shutil.which("git")
    if git_executable is None:
        pytest.skip("git is required for the canonical diff integration test")
    root = tmp_path / "vault"
    (root / "Projects").mkdir(parents=True)
    note = root / "Projects" / "Alpha.md"
    original = b"A\nkeep\nold\nremove"
    replacement = b"A\nkeep\nnew"
    note.write_bytes(original)

    def git(*arguments: str) -> None:
        subprocess.run(
            [git_executable, "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "--quiet")
    git("config", "user.name", "Probe")
    git("config", "user.email", "probe@example.com")
    git("add", "--", "Projects/Alpha.md")
    git("commit", "--quiet", "-m", "base")

    state = type(
        "SyncState",
        (),
        {
            "load_knowledge_vault_synchronized_at": lambda self: NOW,
            "save_knowledge_vault_synchronized_at": lambda self, value: None,
        },
    )()
    synchronizer = SubprocessVaultSynchronizer(
        git_executable=Path(git_executable),
        ssh_executable=Path(git_executable),
        ssh_config_path=tmp_path / "ssh-config",
        known_hosts_path=tmp_path / "known-hosts",
        synchronization_state=state,
    )

    rendered = synchronizer.render_diff(
        root,
        {"Projects/Alpha.md": original.decode("utf-8")},
        {"Projects/Alpha.md": replacement.decode("utf-8")},
    )
    note.write_bytes(replacement)
    synchronizer.stage(root, ("Projects/Alpha.md",))
    staged = synchronizer.staged_diff(root)

    assert rendered == staged
    assert r"\ No newline at end of file" in rendered
    assert "diff --git " not in rendered


def test_agents_adapter_returns_a_vault_write_intent_for_broker_preparation() -> None:
    def run_sync(_agent: object, _text: str, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I prepared the exact note patch for approval.",
                proposal=AgentsSdkProposal(
                    kind="knowledge_vault_write",
                    preview="model preview is not authoritative",
                    payload={
                        "changes": {"Projects/Alpha.md": "# Alpha\n\nStatus: model\n"}
                    },
                ),
            )
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=lambda **kwargs: kwargs,
        reasoning_factory=lambda **kwargs: kwargs,
        run_config_factory=lambda **kwargs: kwargs,
        vault_write_enabled=True,
    )

    result = adapter.run(_orchestration_request())

    assert result.proposal is None
    assert result.proposal_intent is not None
    assert result.proposal_intent.kind == "knowledge_vault_write"
    assert result.proposal_intent.payload == {
        "changes": {"Projects/Alpha.md": "# Alpha\n\nStatus: model\n"}
    }


def test_broker_prepares_agents_vault_intent_before_freezing_exact_action(
    tmp_path: Path,
) -> None:
    connector, repository = _connector(tmp_path)

    def run_sync(_agent: object, _text: str, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I prepared the note change for approval.",
                proposal=AgentsSdkProposal(
                    kind="knowledge_vault_write",
                    preview="untrusted model preview",
                    payload={
                        "changes": {
                            "Projects/Alpha.md": "# Alpha\n\nStatus: approved\n"
                        }
                    },
                ),
            )
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=lambda **kwargs: kwargs,
        reasoning_factory=lambda **kwargs: kwargs,
        run_config_factory=lambda **kwargs: kwargs,
        vault_write_enabled=True,
    )
    router = RoutedActionDispatcher(
        terminal=ControlledActionDispatcher(),
        gmail=ControlledActionDispatcher(),
        gmail_lifecycle=_IdentityLifecycle(),
        vault=connector,
        vault_lifecycle=connector,
    )
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.test",
        signing_secret=b"ticket24-broker-boundary-secret",
        now=NOW,
        id_prefix="ticket24-broker-boundary",
        orchestration=adapter,  # type: ignore[arg-type]
        action_dispatcher=router,
        action_lifecycle=router,
        vault_write_proposal_preparer=connector,
    )

    def receive(text: str, suffix: str):
        return components.receiver.receive(
            SignedInboundEvent.from_message(
                InboundMessage(
                    event_type="message.received",
                    session_id="session.test",
                    event_id=f"event-{suffix}",
                    message_id=f"message-{suffix}",
                    sender_id="operator.test",
                    chat_id="operator.test",
                    chat_type="direct",
                    message_type="text",
                    from_me=False,
                    text=text,
                ),
                b"ticket24-broker-boundary-secret",
            )
        )

    assert receive("change Alpha", "1").disposition == "pending_action"
    assert repository.synchronize_calls == 1
    pending = components.broker.current_pending_action
    assert pending is not None
    assert "Base commit: " + BASE in (pending.preview or "")
    assert receive("yes", "2").disposition == "action_dispatched"
    assert len(repository.commit_calls) == 1
    assert len(repository.push_calls) == 1


def test_agents_adapter_rejects_model_supplied_commit_metadata() -> None:
    def run_sync(_agent: object, _text: str, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I prepared the exact note patch for approval.",
                proposal=AgentsSdkProposal(
                    kind="knowledge_vault_write",
                    preview="model preview is not authoritative",
                    payload={
                        "changes": {"Projects/Alpha.md": "# Alpha\n\nStatus: model\n"},
                        "commit_subject": "jarvis: leaked conversation text",
                    },
                ),
            )
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=lambda **kwargs: kwargs,
        reasoning_factory=lambda **kwargs: kwargs,
        run_config_factory=lambda **kwargs: kwargs,
        vault_write_enabled=True,
    )

    with pytest.raises(OrchestrationAdapterError, match="unexpected shape"):
        adapter.run(_orchestration_request())
