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


def test_routed_broker_dispatches_one_exact_vault_write_after_approval(
    tmp_path: Path,
) -> None:
    connector, repository = _connector(tmp_path)
    router = RoutedActionDispatcher(
        terminal=ControlledActionDispatcher(),
        gmail=ControlledActionDispatcher(),
        gmail_lifecycle=_IdentityLifecycle(),
        vault=connector,
        vault_lifecycle=connector,
    )
    orchestration = ControlledOrchestrationAdapter(
        proposal_factory=lambda request: connector.propose(
            request_id=request.state.request_id,
            changes={"Projects/Alpha.md": "# Alpha\n\nStatus: approved\n"},
        )
    )
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.test",
        signing_secret=b"ticket24-secret",
        now=NOW,
        id_prefix="ticket24",
        orchestration=orchestration,
        action_dispatcher=router,
        action_lifecycle=router,
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
                b"ticket24-secret",
            )
        )

    assert receive("change Alpha", "1").disposition == "pending_action"
    assert receive("yes", "2").disposition == "action_dispatched"
    assert (tmp_path / "Projects" / "Alpha.md").read_text(encoding="utf-8") == (
        "# Alpha\n\nStatus: approved\n"
    )
    assert len(repository.commit_calls) == 1
    assert len(repository.push_calls) == 1
    acknowledgements = [
        reply
        for reply in components.outbound.sent
        if "completed successfully" in reply.body
    ]
    assert len(acknowledgements) == 1
    assert "knowledge vault action completed successfully" in acknowledgements[0].body
    assert "no retry" in acknowledgements[0].body.lower()


def test_vault_write_unknown_push_gets_one_terminal_unknown_ack(tmp_path: Path) -> None:
    class UnknownPushRepository(ControlledVaultWriteRepository):
        def push(self, *args: object, **kwargs: object) -> None:
            self.push_calls.append(
                {
                    "expected_base": kwargs["expected_base"],
                    "commit_id": kwargs["commit_id"],
                }
            )
            raise VaultPushUnknownOutcome("push response was interrupted")

    repository = UnknownPushRepository(current_commit=BASE, remote_commit=BASE)
    connector, _ = _connector(tmp_path, repository=repository)
    router = RoutedActionDispatcher(
        terminal=ControlledActionDispatcher(),
        gmail=ControlledActionDispatcher(),
        gmail_lifecycle=_IdentityLifecycle(),
        vault=connector,
        vault_lifecycle=connector,
    )
    orchestration = ControlledOrchestrationAdapter(
        proposal_factory=lambda request: connector.propose(
            request_id=request.state.request_id,
            changes={"Projects/Alpha.md": "# Alpha\n\nStatus: approved\n"},
        )
    )
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.test",
        signing_secret=b"ticket24-secret",
        now=NOW,
        id_prefix="ticket24-unknown",
        orchestration=orchestration,
        action_dispatcher=router,
        action_lifecycle=router,
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
                b"ticket24-secret",
            )
        )

    assert receive("change Alpha", "1").disposition == "pending_action"
    result = receive("yes", "2")

    assert result.disposition == "action_dispatch_unknown"
    acknowledgements = [
        reply
        for reply in components.outbound.sent
        if "unknown provider outcome" in reply.body
    ]
    assert len(acknowledgements) == 1
    assert "knowledge vault action" in acknowledgements[0].body
    assert "no retry" in acknowledgements[0].body.lower()


def test_subprocess_vault_edge_uses_fetch_only_verification_and_normal_push(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run_process(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((arguments, kwargs))
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{BASE}\n", stderr="")
        if arguments[-2:] == ["rev-parse", "FETCH_HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{BASE}\n", stderr="")
        if "symbolic-ref" in arguments:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if "diff" in arguments:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "diff --git a/Projects/Alpha.md b/Projects/Alpha.md\n"
                    "index 1111111..2222222 100644\n"
                    "--- a/Projects/Alpha.md\n"
                    "+++ b/Projects/Alpha.md\n"
                    "@@ -1 +1 @@\n"
                    "--- old-looking removed\n"
                    "+++ new-looking added\n"
                ),
                stderr="",
            )
        if "commit" in arguments:
            return SimpleNamespace(returncode=0, stdout="[main 123]\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    state = type(
        "SyncState",
        (),
        {
            "load_knowledge_vault_synchronized_at": lambda self: NOW,
            "save_knowledge_vault_synchronized_at": lambda self, value: None,
        },
    )()
    synchronizer = SubprocessVaultSynchronizer(
        git_executable=PurePosixPath("/usr/bin/git"),
        ssh_executable=PurePosixPath("/usr/bin/ssh"),
        ssh_config_path=PurePosixPath("/etc/jarvis/vault-ssh-config"),
        known_hosts_path=PurePosixPath("/etc/jarvis/vault-known-hosts"),
        synchronization_state=state,
        run_process=run_process,
    )

    assert synchronizer.current_commit(tmp_path) == BASE
    assert synchronizer.fetch_remote_commit(tmp_path) == BASE
    synchronizer.stage(tmp_path, ("Projects/Alpha.md",))
    staged_diff = synchronizer.staged_diff(tmp_path)
    assert staged_diff.startswith("--- a/Projects/Alpha.md\n+++ b/Projects/Alpha.md\n")
    assert "--- old-looking removed\n" in staged_diff
    assert "+++ new-looking added\n" in staged_diff
    commit_id = synchronizer.commit(
        tmp_path,
        author_name="Jarvis",
        author_email="jarvis@samikayyal.com",
        subject="jarvis: update knowledge vault",
        body="Request ID: request-24",
    )
    assert commit_id == BASE
    synchronizer.push(tmp_path, expected_base=BASE, commit_id=BASE)

    push_commands = [args for args, _kwargs in calls if "push" in args]
    assert push_commands == [
        [
            "/usr/bin/git",
            "-C",
            str(tmp_path),
            "push",
            "--porcelain",
            "origin",
            "HEAD:refs/heads/main",
        ]
    ]
    assert all("--force" not in args for args, _kwargs in calls)
    assert all("rebase" not in args and "merge" not in args for args, _kwargs in calls)


@pytest.mark.parametrize("failure_mode", ("timeout", "generic"))
def test_subprocess_ambiguous_push_outcomes_are_unknown_and_never_retried(
    tmp_path: Path, failure_mode: str
) -> None:
    calls: list[list[str]] = []

    def run_process(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(arguments)
        if "symbolic-ref" in arguments:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{BASE}\n", stderr="")
        if "push" in arguments:
            if failure_mode == "timeout":
                raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="fatal: remote connection dropped after negotiation",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    state = type(
        "SyncState",
        (),
        {
            "load_knowledge_vault_synchronized_at": lambda self: NOW,
            "save_knowledge_vault_synchronized_at": lambda self, value: None,
        },
    )()
    synchronizer = SubprocessVaultSynchronizer(
        git_executable=PurePosixPath("/usr/bin/git"),
        ssh_executable=PurePosixPath("/usr/bin/ssh"),
        ssh_config_path=PurePosixPath("/etc/jarvis/vault-ssh-config"),
        known_hosts_path=PurePosixPath("/etc/jarvis/vault-known-hosts"),
        synchronization_state=state,
        run_process=run_process,
    )

    with pytest.raises(VaultPushUnknownOutcome):
        synchronizer.push(tmp_path, expected_base=BASE, commit_id=BASE)

    assert len([args for args in calls if "push" in args]) == 1


def test_subprocess_push_launch_failure_is_provably_pre_dispatch(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run_process(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(arguments)
        if "symbolic-ref" in arguments:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{BASE}\n", stderr="")
        if "push" in arguments:
            raise OSError("temporary process-launch failure")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    state = type(
        "SyncState",
        (),
        {
            "load_knowledge_vault_synchronized_at": lambda self: NOW,
            "save_knowledge_vault_synchronized_at": lambda self, value: None,
        },
    )()
    synchronizer = SubprocessVaultSynchronizer(
        git_executable=PurePosixPath("/usr/bin/git"),
        ssh_executable=PurePosixPath("/usr/bin/ssh"),
        ssh_config_path=PurePosixPath("/etc/jarvis-vault-ssh-config"),
        known_hosts_path=PurePosixPath("/etc/jarvis-vault-known-hosts"),
        synchronization_state=state,
        run_process=run_process,
    )

    with pytest.raises(VaultPushPreDispatchFailure):
        synchronizer.push(tmp_path, expected_base=BASE, commit_id=BASE)

    assert len([args for args in calls if "push" in args]) == 1


def test_provably_pre_dispatch_push_failure_gets_only_one_bounded_retry(
    tmp_path: Path,
) -> None:
    repository = ControlledVaultWriteRepository(
        current_commit=BASE,
        remote_commit=BASE,
        push_remote_unavailable=True,
    )
    connector, _repository = _connector(tmp_path, repository=repository)
    proposal = connector.propose(
        request_id="request-pre-dispatch-retry",
        changes={"Projects/Alpha.md": "# Alpha\n\nStatus: retry\n"},
    )

    with pytest.raises(ActionDispatcherError, match="manual recovery"):
        connector.dispatch(proposal)

    assert len(repository.push_calls) == 2
