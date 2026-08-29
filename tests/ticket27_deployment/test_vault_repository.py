"""Ticket 27 production vault repository composition tests."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis_control_plane.knowledge_vault import VaultRepositoryConflict
from jarvis_control_plane.vault_repository import SubprocessVaultRepository


def _git(executable: Path, root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        (str(executable), "-C", str(root), *arguments),
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def test_production_vault_diff_matches_exact_staged_diff_and_pushes(
    tmp_path: Path,
) -> None:
    discovered = shutil.which("git")
    if discovered is None:
        pytest.skip("Git is unavailable")
    executable = Path(discovered).resolve()
    remote = tmp_path / "remote.git"
    vault = tmp_path / "vault"
    subprocess.run(
        (str(executable), "init", "--bare", str(remote)),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        (str(executable), "clone", str(remote), str(vault)),
        check=True,
        capture_output=True,
    )
    _git(executable, vault, "config", "user.name", "Test")
    _git(executable, vault, "config", "user.email", "test@example.invalid")
    (vault / "Notes").mkdir()
    (vault / "Notes" / "one.md").write_text("old\n", encoding="utf-8")
    _git(executable, vault, "add", "--", "Notes/one.md")
    _git(executable, vault, "commit", "-m", "initial")
    _git(executable, vault, "branch", "-M", "main")
    _git(executable, vault, "push", "-u", "origin", "main")

    repository = SubprocessVaultRepository(
        git_executable=executable,
        ssh_executable=executable,
        ssh_config_path=(tmp_path / "ssh_config").resolve(),
        known_hosts_path=(tmp_path / "known_hosts").resolve(),
    )
    repository.validate_remote(vault, str(remote))
    with pytest.raises(VaultRepositoryConflict, match="active configuration"):
        repository.validate_remote(vault, str(tmp_path / "other.git"))
    assert repository._environment["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert repository._environment["GIT_CONFIG_VALUE_0"] == "/dev/null"
    base = repository.current_commit(vault)
    patch = repository.render_diff(
        vault,
        {"Notes/one.md": "old\n"},
        {"Notes/one.md": "new\n"},
    )
    (vault / "Notes" / "one.md").write_text("new\n", encoding="utf-8")
    repository.stage(vault, ("Notes/one.md",))

    assert repository.staged_diff(vault) == patch

    commit = repository.commit(
        vault,
        author_name="Jarvis",
        author_email="jarvis@example.invalid",
        subject="jarvis: update knowledge vault",
        body="request: test",
    )
    repository.push(vault, expected_base=base, commit_id=commit)

    assert repository.fetch_remote_commit(vault) == commit
    assert repository.synchronize(
        vault, now=datetime(2026, 8, 11, tzinfo=UTC)
    ) == datetime(2026, 8, 11, tzinfo=UTC)
