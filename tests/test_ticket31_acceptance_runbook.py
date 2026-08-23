from __future__ import annotations

from pathlib import Path


def _runbook() -> str:
    root = Path(__file__).parents[1]
    return (root / "deployment/google-vault-acceptance-runbook.md").read_text()


def test_runbook_requires_real_supervision_and_separate_exact_approvals() -> None:
    runbook = _runbook()
    assert "are not production proof" in runbook
    assert "operator and a second reviewer" in runbook
    assert "Agreement to run this worksheet is not approval" in runbook
    assert "Reply exactly `yes`" in runbook
    assert "leave this gate blocked" in runbook


def test_runbook_covers_every_in_scope_ticket31_gate() -> None:
    runbook = _runbook()
    required = (
        "Installed revision",
        "Connected Google identity",
        "Bounded Gmail list/get",
        "Broker disconnect",
        "altered Gmail approval",
        "exact operator-owned labeled Gmail",
        "Replaying the old exact approval",
        "Gmail-only application-level post-dispatch failpoint",
        "Deterministic synchronized vault read",
        "exact Markdown append",
        "Calendar requests",
        "No active request",
    )
    assert all(item in runbook for item in required)
    assert "one normal commit and push" in runbook
    assert "provider first" in runbook
    assert "Git heads alone are not an acknowledgement" in runbook


def test_calendar_is_an_explicit_v1_refusal_not_an_acceptance_capability() -> None:
    runbook = _runbook()
    assert "Calendar is not a v1 capability" in runbook
    assert "protocol exposes no Calendar operation" in runbook
    assert "orchestration has no Calendar tool or proposal kind" in runbook
    assert "broker has no Calendar dispatcher route" in runbook
    assert "A Calendar scope is a v1 hard stop" in runbook
    assert "Calendar refusal must be tested with Luna at medium or high" in runbook
    assert "--access calendar-write" not in runbook
    assert 'service = "calendar"' not in runbook
    assert "Calendar exact approval" not in runbook


def test_unknown_outcome_is_gmail_only_and_durably_retired() -> None:
    normalized = " ".join(_runbook().split())
    for field in ("enabled", "service", "operation", "action_id", "review_id"):
        assert field in normalized
    assert 'service = "gmail"' in normalized
    assert 'operation = "gmail_send"' in normalized
    assert 'action_id = ""' in normalized
    assert "Never guess, copy, precompute" in normalized
    assert "durable binding marker" in normalized
    assert "Never retry" in normalized
    assert "consumed marker remains inert" in normalized
    assert "target is retired" in normalized


def test_runbook_keeps_privileged_and_provider_boundaries_human_owned() -> None:
    runbook = _runbook()
    forbidden_commands = (
        "git push --force",
        "git reset --hard",
        "git rebase ",
        "docker compose down --volumes",
        "cat /etc/jarvis/credentials",
        "printenv",
    )
    assert all(command not in runbook for command in forbidden_commands)
    assert "does not authorize activation, deployment" in runbook
    assert "direct-provider mutation" in runbook
    assert "Do not run a manual `git push`" in runbook
    assert "Never expose OAuth URLs" in runbook


def test_readme_links_the_ticket31_runbook_after_activation() -> None:
    root = Path(__file__).parents[1]
    readme = (root / "deployment/README.md").read_text()
    assert "google-vault-acceptance-runbook.md" in readme
