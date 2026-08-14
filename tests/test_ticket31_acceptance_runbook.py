from __future__ import annotations

from pathlib import Path


def _runbook() -> str:
    root = Path(__file__).parents[1]
    return (root / "deployment/google-vault-acceptance-runbook.md").read_text()


def test_runbook_requires_real_supervision_and_separate_exact_approvals() -> None:
    runbook = _runbook()
    assert "are not production proof" in runbook
    assert "operator and a\nsecond reviewer" in runbook
    assert "Agreement to run this worksheet is not\n  approval" in runbook
    assert "Reply exactly `yes`" in runbook
    assert "leave the ticket `ready-for-human`" in runbook


def test_runbook_covers_every_ticket31_real_system_gate() -> None:
    runbook = _runbook()
    required_rows = (
        "| Baseline scopes |",
        "| Gmail reads |",
        "| Calendar reads |",
        "| Drive reads |",
        "| Disconnect/reconnect |",
        "| Gmail altered approval |",
        "| Gmail exact approval |",
        "| Gmail replay |",
        "| Gmail unknown outcome |",
        "| Calendar altered approval |",
        "| Calendar exact approval |",
        "| Calendar replay |",
        "| Calendar unknown outcome |",
        "| Calendar stale generation |",
        "| Vault read |",
        "| Vault exact write |",
        "| Google exclusions |",
        "| Vault exclusions |",
        "| Final reconciliation |",
    )
    assert all(row in runbook for row in required_rows)
    assert "one normal push" in runbook
    assert "actual\nTo, Cc, Bcc, subject, body, MIME type" in runbook
    assert "actual event identity, calendar,\nsummary, description, location" in runbook
    assert runbook.count("same complete material-field set") == 2
    assert "actual local and remote\ncommit subject is the fixed `jarvis:` subject" in runbook
    assert "author identity equals the\nconfigured identity" in runbook
    assert "finish\nor cancel it without approving a side effect" in runbook
    assert "use this dedicated negative sequence instead\nof section 3" in runbook
    assert "must never create a proposal or connector\ndispatch" in runbook


def test_runbook_does_not_document_a_destructive_or_secret_bypass() -> None:
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
    assert "Never expose the\nsigning secret" in runbook
    assert "Do not run a manual `git push`" in runbook
    assert "do not retry" in runbook.lower()


def test_readme_links_the_ticket31_runbook_after_activation() -> None:
    root = Path(__file__).parents[1]
    readme = (root / "deployment/README.md").read_text()
    assert "google-vault-acceptance-runbook.md" in readme
