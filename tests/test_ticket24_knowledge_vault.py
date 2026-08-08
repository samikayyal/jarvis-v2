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


def test_proposal_freezes_base_paths_complete_diff_and_commit_metadata(
    tmp_path: Path,
) -> None:
    connector, repository = _connector(tmp_path)

    proposal = connector.propose(
        request_id="request-24",
        changes={
            "Projects/Alpha.md": "# Alpha\n\nStatus: active\n",
            "Projects/Beta.md": "# Beta\n\nStatus: new\n",
        },
    )

    payload = json.loads(proposal.payload)
    assert proposal.kind == "knowledge_vault_write"
    assert payload["schema"] == "knowledge_vault_write_v1"
    assert payload["base_commit"] == BASE
    assert payload["paths"] == ["Projects/Alpha.md", "Projects/Beta.md"]
    assert [change["operation"] for change in payload["changes"]] == [
        "modify",
        "create",
    ]
    assert "--- a/Projects/Alpha.md" in payload["diff"]
    assert "+++ b/Projects/Alpha.md" in payload["diff"]
    assert "--- /dev/null" in payload["diff"]
    assert "+++ b/Projects/Beta.md" in payload["diff"]
    assert payload["commit_identity"] == {
        "name": "Jarvis",
        "email": "jarvis@samikayyal.com",
    }
    assert payload["commit_subject"] == "jarvis: update knowledge vault"
    assert "Projects/Alpha.md" in payload["commit_body"]
    assert "request-24" in payload["commit_body"]
    assert "approval will commit and push precisely this patch" in proposal.preview
    assert repository.synchronize_calls == 1

    parsed = VaultWriteRequest.from_proposal(proposal)
    assert parsed.base_commit == BASE
    assert tuple(change.path for change in parsed.changes) == (
        "Projects/Alpha.md",
        "Projects/Beta.md",
    )
    assert parsed.patch == payload["diff"]


@pytest.mark.parametrize(
    "path",
    (
        "../outside.md",
        "Projects/../outside.md",
        ".obsidian/app.json",
        "attachments/file.md",
        "Projects/linked.md",
        "Projects/not-markdown.txt",
    ),
)
def test_proposal_rejects_paths_outside_configured_ordinary_markdown_notes(
    tmp_path: Path, path: str
) -> None:
    connector, _repository = _connector(tmp_path)
    if path.endswith("linked.md"):
        (tmp_path / "outside.md").write_text("outside\n", encoding="utf-8")
        (tmp_path / path).symlink_to(tmp_path / "outside.md")

    with pytest.raises(VaultWriteError, match="ordinary knowledge-vault note"):
        connector.propose(request_id="request-path", changes={path: "content\n"})


def test_proposal_rejects_notes_inside_a_nested_git_repository(tmp_path: Path) -> None:
    connector, _repository = _connector(tmp_path)
    nested = tmp_path / "Projects" / "Nested"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")

    with pytest.raises(VaultWriteError, match="ordinary knowledge-vault note"):
        connector.propose(
            request_id="request-nested-repository",
            changes={"Projects/Nested/Note.md": "content\n"},
        )


def test_proposal_rejects_dirty_or_remote_unavailable_clone_without_stale_write(
    tmp_path: Path,
) -> None:
    dirty = ControlledVaultWriteRepository(
        current_commit=BASE, remote_commit=BASE, clean=False
    )
    connector, _repository = _connector(tmp_path, repository=dirty)
    with pytest.raises(VaultWriteError, match="clean"):
        connector.propose(request_id="request-dirty", changes={"Projects/A.md": "x\n"})

    unavailable = ControlledVaultWriteRepository(
        current_commit=BASE,
        remote_commit=BASE,
        synchronize_failure="remote unavailable",
    )
    connector, _repository = _connector(
        tmp_path / "unavailable", repository=unavailable
    )
    with pytest.raises(VaultWriteError, match="synchronization"):
        connector.propose(
            request_id="request-unavailable", changes={"Projects/A.md": "x\n"}
        )


def test_proposal_rejects_noop_and_explicit_delete_or_rename_operations(
    tmp_path: Path,
) -> None:
    connector, _repository = _connector(tmp_path)
    with pytest.raises(VaultWriteError, match="no changes"):
        connector.propose(
            request_id="request-noop",
            changes={"Projects/Alpha.md": "# Alpha\n\nStatus: draft\n"},
        )

    with pytest.raises(ValueError, match="operation"):
        VaultWriteProposal.create(
            action_id="action-delete",
            request_id="request-delete",
            base_commit=BASE,
            changes=(
                {
                    "path": "Projects/Alpha.md",
                    "operation": "delete",
                    "content": "",
                },
            ),
            diff="--- a/Projects/Alpha.md\n+++ /dev/null\n",
            commit_subject="jarvis: delete note",
            commit_body="not allowed",
        )


def test_dispatch_applies_and_verifies_only_the_frozen_patch_then_commits_and_pushes(
    tmp_path: Path,
) -> None:
    connector, repository = _connector(tmp_path)
    proposal = connector.propose(
        request_id="request-dispatch",
        changes={"Projects/Alpha.md": "# Alpha\n\nStatus: shipped\n"},
    )

    result = connector.prepare(proposal).run()

    assert result.commit_id == "0" * 39 + "1"
    assert result.paths == ("Projects/Alpha.md",)
    assert (tmp_path / "Projects" / "Alpha.md").read_text(encoding="utf-8") == (
        "# Alpha\n\nStatus: shipped\n"
    )
    assert repository.commit_calls == [
        {
            "author_name": "Jarvis",
            "author_email": "jarvis@samikayyal.com",
            "subject": "jarvis: update knowledge vault",
            "body": "Changed knowledge-vault note paths:\n- Projects/Alpha.md\nRequest ID: request-dispatch",
        }
    ]
    assert repository.push_calls == [
        {"expected_base": BASE, "commit_id": result.commit_id}
    ]


def test_changed_base_or_dirty_state_stops_before_any_file_or_commit_change(
    tmp_path: Path,
) -> None:
    connector, repository = _connector(tmp_path)
    proposal = connector.propose(
        request_id="request-race",
        changes={"Projects/Alpha.md": "# Alpha\n\nStatus: changed\n"},
    )
    repository.advance_remote("b" * 40)

    with pytest.raises(ActionDispatcherError, match="base"):
        connector.dispatch(proposal)

    assert (tmp_path / "Projects" / "Alpha.md").read_text(encoding="utf-8") == (
        "# Alpha\n\nStatus: draft\n"
    )
    assert repository.commit_calls == []
    assert repository.stage_calls == []

    follow_up = connector.propose(
        request_id="request-after-invalidated-proposal",
        changes={"Projects/Alpha.md": "# Alpha\n\nStatus: retry\n"},
    )
    assert connector.dispatch(follow_up).paths == ("Projects/Alpha.md",)

    connector, repository = _connector(
        tmp_path / "dirty",
        repository=ControlledVaultWriteRepository(
            current_commit=BASE, remote_commit=BASE
        ),
    )
    proposal = connector.propose(
        request_id="request-dirty-dispatch",
        changes={"Projects/Alpha.md": "# Alpha\n\nStatus: changed\n"},
    )
    repository.clean = False
    with pytest.raises(ActionDispatcherError, match="clean"):
        connector.dispatch(proposal)
    assert repository.commit_calls == []


def test_staged_diff_mismatch_stops_and_post_commit_push_conflict_blocks_later_writes(
    tmp_path: Path,
) -> None:
    repository = ControlledVaultWriteRepository(
        current_commit=BASE,
        remote_commit=BASE,
        staged_diff_override="a different patch\n",
    )
    connector, _repository = _connector(tmp_path, repository=repository)
    proposal = connector.propose(
        request_id="request-staged-mismatch",
        changes={"Projects/Alpha.md": "# Alpha\n\nStatus: staged\n"},
    )
    with pytest.raises(ActionDispatcherError, match="staged"):
        connector.dispatch(proposal)
    assert repository.commit_calls == []

    repository = ControlledVaultWriteRepository(
        current_commit=BASE,
        remote_commit=BASE,
    )
    connector, _repository = _connector(
        tmp_path / "trailing-space", repository=repository
    )
    proposal = connector.propose(
        request_id="request-trailing-space",
        changes={"Projects/Alpha.md": "# Alpha\n\nStatus: staged  \n"},
    )
    repository.staged_diff_override = json.loads(proposal.payload)["diff"].replace(
        "staged  ", "staged"
    )
    with pytest.raises(ActionDispatcherError, match="staged"):
        connector.dispatch(proposal)
    assert repository.commit_calls == []

    repository = ControlledVaultWriteRepository(
        current_commit=BASE,
        remote_commit=BASE,
        push_failure="non-fast-forward",
    )
    connector, _repository = _connector(
        tmp_path / "push-conflict", repository=repository
    )
    proposal = connector.propose(
        request_id="request-push-conflict",
        changes={"Projects/Alpha.md": "# Alpha\n\nStatus: pushed?\n"},
    )
    with pytest.raises(ActionDispatcherError, match="manual recovery"):
        connector.dispatch(proposal)
    assert len(repository.commit_calls) == 1
    with pytest.raises(VaultWriteError, match="blocked pending manual recovery"):
        connector.propose(
            request_id="request-after-conflict",
            changes={"Projects/Alpha.md": "# Alpha\n\nStatus: later\n"},
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


def test_staged_verification_rejects_an_unrelated_index_path(
    tmp_path: Path,
) -> None:
    repository = _UnrelatedStagedRepository(
        current_commit=BASE,
        remote_commit=BASE,
    )
    connector, _repository = _connector(tmp_path, repository=repository)
    proposal = connector.propose(
        request_id="request-unrelated-index",
        changes={"Projects/Alpha.md": "# Alpha\n\nStatus: staged\n"},
    )
    proposal_patch = json.loads(proposal.payload)["diff"]
    repository.path_scoped_diff = proposal_patch
    repository.complete_index_diff = (
        proposal_patch
        + "--- a/Projects/Other.md\n"
        + "+++ b/Projects/Other.md\n"
        + "@@ -1 +1 @@\n"
        + "-old\n"
        + "+unrelated\n"
    )

    with pytest.raises(ActionDispatcherError, match="staged"):
        connector.dispatch(proposal)

    assert repository.staged_diff_requests == [()]
    assert repository.commit_calls == []


def test_dispatch_rejects_a_commit_subject_that_is_not_configured(
    tmp_path: Path,
) -> None:
    connector, _repository = _connector(tmp_path)
    change = VaultWriteChange(
        path="Projects/Alpha.md",
        operation="modify",
        content="# Alpha\n\nStatus: forged\n",
    )
    proposal = VaultWriteProposal.create(
        action_id="request-subject:proposal",
        request_id="request-subject",
        base_commit=BASE,
        changes=(change,),
        diff=render_vault_unified_diff(
            {"Projects/Alpha.md": "# Alpha\n\nStatus: draft\n"}, (change,)
        ),
        commit_subject="jarvis: injected metadata",
        commit_body="Changed knowledge-vault note paths:\n- Projects/Alpha.md\nRequest ID: request-subject",
    )

    with pytest.raises(ActionDispatcherError, match="subject changed"):
        connector.dispatch(proposal)


def test_dispatch_rejects_a_frozen_operation_that_does_not_match_the_live_base(
    tmp_path: Path,
) -> None:
    connector, _repository = _connector(tmp_path)
    change = VaultWriteChange(
        path="Projects/Alpha.md",
        operation="create",
        content="# Alpha\n\nStatus: forged\n",
    )
    proposal = VaultWriteProposal.create(
        action_id="request-operation:proposal",
        request_id="request-operation",
        base_commit=BASE,
        changes=(change,),
        diff=render_vault_unified_diff(
            {"Projects/Alpha.md": "# Alpha\n\nStatus: draft\n"}, (change,)
        ),
        commit_body="Changed knowledge-vault note paths:\n- Projects/Alpha.md\nRequest ID: request-operation",
    )

    with pytest.raises(ActionDispatcherError, match="operation"):
        connector.dispatch(proposal)


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
