from __future__ import annotations

from pathlib import Path


def _runbook() -> str:
    root = Path(__file__).parents[1]
    return (root / "deployment/terminal-codex-acceptance-runbook.md").read_text()


def test_runbook_requires_real_supervision_and_separate_authority() -> None:
    runbook = _runbook()
    assert "are not production proof" in runbook
    assert "authorized operator and a second reviewer" in runbook
    assert "Agreement to run the worksheet is not approval" in runbook
    assert "human administrator must separately" in runbook
    assert "approve every worker stop/start" in runbook
    assert "stays `ready-for-human`" in runbook


def test_runbook_covers_ticket32_host_and_worker_matrix() -> None:
    runbook = _runbook()
    required = (
        "host-neutral natural-language request selects Ubuntu",
        "explicitly dependent on the authorized Windows laptop selects Windows",
        "without queueing or Ubuntu substitution",
        "same registered Windows identity reconnects",
        "invalid Windows worker identity is rejected",
        "without displacing the live registration",
    )
    assert all(item in runbook for item in required)


def test_runbook_covers_terminal_authority_and_bounded_execution() -> None:
    runbook = _runbook()
    required = (
        "safe Ubuntu read",
        "ordinary eligible terminal action rejects an altered approval",
        "mandatory-fresh action offers only",
        "one session permission and one persistent permission",
        "`/revoke` takes effect before acknowledgement",
        "pending action expires without execution after ten minutes",
        "fixed stdout/stderr caps",
        "terminal timeout",
        "process-tree cancellation",
        "partial compound outcome",
        "unknown outcome is never retried",
    )
    assert all(item in runbook for item in required)
    assert "reply exactly `1`" in runbook


def test_runbook_keeps_codex_bounded_and_independently_verified() -> None:
    runbook = _runbook()
    required = (
        "workspace prompt-injection fixture",
        "bounded read-only inspection or test",
        "independently verifies the workspace state",
        "exact broker-owned, allowlisted workspace-preparation proposal",
        "`workspace-write` plus `on-request`",
        "Push, history rewriting, `danger-full-access`",
        "trust-critical activation",
        "do not substitute an administrative or direct Python",
    )
    assert all(item in runbook for item in required)
    assert "Codex prose is not evidence" in runbook


def test_runbook_does_not_authorize_unsafe_or_direct_production_mutation() -> None:
    runbook = _runbook()
    forbidden = (
        "git push --force",
        "git reset --hard",
        "danger-full-access --yes",
        "docker compose down --volumes",
        "cat /etc/jarvis/credentials",
        "printenv",
    )
    assert all(item not in runbook for item in forbidden)
    assert "does not authorize deployment" in runbook
    assert "Never replace the production worker certificate" in runbook
    assert "Never retry the action automatically or manually" in runbook
    assert "Never call the Codex adapter directly" in runbook


def test_readme_links_the_ticket32_runbook_after_activation() -> None:
    root = Path(__file__).parents[1]
    readme = (root / "deployment/README.md").read_text()
    assert "terminal-codex-acceptance-runbook.md" in readme
