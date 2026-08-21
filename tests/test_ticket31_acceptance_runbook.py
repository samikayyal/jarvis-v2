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
    assert "event ID returned by the create" in runbook
    assert "compare an event ID with the frozen proposal only for\nan update" in runbook
    assert (
        "notification choice from the\nprotected provider request/audit trace"
        in runbook
    )
    assert runbook.count("same complete material-field set") == 2
    assert (
        "actual local and remote\ncommit subject is the fixed `jarvis:` subject"
        in runbook
    )
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


def test_repair_rerun_contract_keeps_gate_dependencies_and_aggregate_explicit() -> None:
    runbook = " ".join(_runbook().split())

    required_phrases = (
        "Gate 03",
        "Calendar list/get grounding",
        "Drive text export returns text",
        "Google unavailable",
        "Gmail success does not cover Calendar or Drive",
        "Gate 08 is Gmail-only",
        "Gates 09-11",
        "Gate 12",
        "Gate 13",
        "Gate 15",
        "terminal completion acknowledgement",
        "outcome-unknown",
        "Gate 18",
        "`fail`, `blocked`, or `deferred`",
    )
    assert all(phrase in runbook for phrase in required_phrases)
    assert (
        "Do not run Calendar mutation gates until the Calendar read boundary is proven"
        in runbook
    )
    assert "If Gate 03 is `fail`, these rows remain `deferred`" in runbook
    assert "Git heads alone are not an acknowledgement" in runbook


def test_unknown_outcome_rows_require_provider_specific_reviewed_failpoints() -> None:
    runbook = " ".join(_runbook().split())

    assert (
        "Gate 08 (Gmail unknown outcome)"
        " only if the operator separately authorizes" in runbook
    )
    assert (
        "Gate 12 (Calendar unknown outcome)"
        " only after the Calendar read and write prerequisites have passed" in runbook
    )
    assert runbook.count("application-level post-dispatch failpoint") >= 2
    assert "do not substitute Gmail's failpoint" in runbook
    assert "transport interruption" in runbook
    assert "container kill" in runbook
    assert "firewall edit" in runbook
    assert "proxy replacement" in runbook


def test_runbook_documents_exact_failpoint_fields_and_retirement() -> None:
    runbook = " ".join(_runbook().split())

    for field in ("enabled", "service", "operation", "action_id", "review_id"):
        assert field in runbook
    assert "root-owned, read-only active configuration" in runbook
    assert "one-shot" in runbook
    assert "all four target fields empty" in runbook
    assert "no pending action or unresolved unknown remains" in runbook


def test_controlled_unknown_outcome_harness_covers_gmail_and_calendar_separately() -> (
    None
):
    root = Path(__file__).parents[1]
    gmail_tests = (root / "tests/test_ticket18_gmail_writes.py").read_text()
    calendar_tests = (root / "tests/test_ticket19_calendar_actions.py").read_text()

    assert (
        "test_gmail_post_dispatch_failpoint_is_unknown_and_replay_free" in gmail_tests
    )
    assert (
        "test_calendar_post_dispatch_failpoint_is_unknown_and_replay_free"
        in calendar_tests
    )
    assert "failpoint" in gmail_tests.lower()
    assert "failpoint" in calendar_tests.lower()
    assert "unknown provider outcome" in gmail_tests
    assert "unknown provider outcome" in calendar_tests


def test_vault_ack_harness_distinguishes_success_and_unknown_outcome() -> None:
    root = Path(__file__).parents[1]
    vault_tests = (root / "tests/test_ticket24_knowledge_vault.py").read_text()
    runbook = " ".join(_runbook().split())

    assert "test_vault_write_unknown_push_gets_one_terminal_unknown_ack" in vault_tests
    assert "completed successfully" in vault_tests
    assert "unknown provider outcome" in vault_tests
    assert "one terminal completion acknowledgement" in runbook
    assert "successful commit-and-push from `outcome-unknown`" in runbook


def test_readme_links_the_ticket31_runbook_after_activation() -> None:
    root = Path(__file__).parents[1]
    readme = (root / "deployment/README.md").read_text()
    assert "google-vault-acceptance-runbook.md" in readme
