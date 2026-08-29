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


def test_repair_rerun_contract_keeps_calendar_dependencies_and_aggregate_strict() -> (
    None
):
    runbook = " ".join(_runbook().split())

    required_phrases = (
        "Gate 03",
        "Gate 08",
        "Gate 09",
        "Gate 10",
        "Gate 11",
        "Gate 12",
        "Gate 13",
        "Gate 15",
        "Gate 18",
        "Calendar action-prepare/write route",
        "exact secondary calendar",
        "complete frozen proposal",
        "credential generation",
        "no pending action and no provider event",
        "Gate 10 remains `deferred`",
        "Gate 11 remains `deferred`",
        "Gate 13 `deferred`",
        "Gate 18 is `pass` only when",
        "`fail`, `blocked`, or `deferred`",
    )
    assert all(phrase in runbook for phrase in required_phrases)
    assert (
        "Do not run Calendar mutation gates until the Calendar read boundary is proven"
        in runbook
    )
    assert "If Gate 09 is `fail`, Gate 10 remains `deferred`" in runbook
    assert "If there is no completed action from Gate 10, Gate 11 remains `deferred`" in runbook
    assert "Git heads alone are not an acknowledgement" in runbook


def test_unknown_outcome_rows_require_separately_reviewed_connector_failpoints() -> (
    None
):
    runbook = " ".join(_runbook().split())

    for field in ("enabled", "service", "operation", "action_id", "review_id"):
        assert field in runbook
    assert "[acceptance_failpoint]" in runbook
    assert 'service = "gmail"' in runbook
    assert 'operation = "gmail_send"' in runbook
    assert 'service = "calendar"' in runbook
    assert 'operation = "insert"' in runbook
    assert 'review_id = "ticket31-calendar-unknown"' in runbook
    assert "gmail_send" in runbook and "gmail_reply" in runbook
    assert "insert" in runbook and "update" in runbook and "patch" in runbook
    assert runbook.count("application-level post-dispatch failpoint") >= 2
    assert "Gmail connector" in runbook
    assert "Calendar connector" in runbook
    assert "after the provider has returned success" in runbook
    assert "before Jarvis writes the terminal acknowledgement" in runbook
    assert "outcome-unknown" in runbook
    assert "zero or one" in runbook
    assert "durable Jarvis reconciliation" in runbook
    assert "one-shot" in runbook
    assert "all four target fields empty" in runbook
    assert "no pending action or unresolved unknown remains" in runbook


def test_calendar_sequence_requires_complete_target_bound_proposals() -> None:
    runbook = " ".join(_runbook().split())

    assert "prepare-only response" in runbook
    assert "exact `calendar_id`" in runbook
    assert "complete event payload" in runbook
    assert "notification choice" in runbook
    assert "action reference" in runbook
    assert "digest" in runbook
    assert "Do not use the primary calendar" in runbook
    assert "do not attempt exact approval, replay, or an alternate Calendar/provider route" in runbook
    assert "old generation must fail before dispatch" in runbook
    assert "same frozen proposal remains the intentional test" in runbook
    assert "no replacement request" in runbook


def test_runbook_keeps_failpoint_arming_outside_activation_and_forbids_interruption() -> (
    None
):
    runbook = _runbook()
    normalized = " ".join(runbook.split())

    assert "does not authorize activation or deployment" in normalized
    assert "does not authorize that configuration change" in runbook
    for forbidden in (
        "docker kill",
        "docker compose down",
        "iptables",
        "firewall-cmd",
    ):
        assert forbidden not in runbook
    assert "does not interrupt transport, a firewall, a proxy, or a container" in (
        normalized
    )
    assert "do not substitute Gmail's failpoint" in runbook


def test_readme_links_the_ticket31_runbook_after_activation() -> None:
    root = Path(__file__).parents[1]
    readme = (root / "deployment/README.md").read_text()
    assert "google-vault-acceptance-runbook.md" in readme
