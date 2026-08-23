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
    assert "status stays `ready-for-human`" in runbook


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
        "Deterministic synchronized vault read",
        "exact Markdown append",
        "A Calendar request is refused",
        "no active request",
    )
    assert all(item in runbook for item in required)
    assert "one normal commit and push" in runbook
    assert "Git heads alone are not an acknowledgement" in runbook


def test_calendar_is_an_explicit_v1_refusal_not_an_acceptance_capability() -> None:
    runbook = _runbook()
    assert "Calendar is not a v1 capability" in runbook
    assert "protocol exposes no Calendar operation" in runbook
    assert "orchestration has no Calendar tool or proposal kind" in runbook
    assert "broker has no Calendar dispatcher route" in runbook
    assert (
        "No configuration allowlist or acceptance failpoint can target Calendar"
        in runbook
    )
    assert "A Calendar scope is a v1 hard stop" in runbook
    assert "`/model gpt-5.6-luna`" in runbook
    assert "`/reasoning high`" in runbook
    assert (
        "without a tool call, proposal, pending action, or provider dispatch" in runbook
    )
    assert "--access calendar-write" not in runbook
    assert 'service = "calendar"' not in runbook
    assert "Calendar exact approval" not in runbook


def test_ticket31_does_not_require_manufactured_unknown_outcomes() -> None:
    runbook = _runbook()
    assert "post-dispatch failpoint" not in runbook
    assert "acceptance_failpoint" not in runbook
    assert 'service = "gmail"' not in runbook
    assert "Require one durable `unknown`" not in runbook
    assert "failure injection" in runbook
    assert "If the push result is ambiguous, stop for manual recovery" in runbook


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
