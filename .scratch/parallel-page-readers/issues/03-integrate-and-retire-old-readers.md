# 03: Integrate the complete workflow and remove the old machinery

**What to build:** Ordinary Jarvis web-assisted requests use the complete concurrent-reader workflow, show useful progress during lengthy research, and finish with concise evidence-aware replies. The superseded reader workflow is removed rather than retained underneath a new interface.

**Blocked by:** 02 — Add concurrent readers and retained follow-ups.

**Status:** complete

- [x] Normal main-agent tool discovery and runtime instructions expose reader start, collection/status, wait, follow-up, cancellation and release coherently. A complete model-and-tool scenario demonstrates delegation, other main work, result synthesis and final cleanup.
- [x] Provide a brief factual progress message after roughly 20 seconds if processing continues, then only on meaningful developments. Progress reflects observable work, does not narrate each action, and stops on cancellation, failure or final completion without racing a late message.
- [x] Preserve clear busy handling for new ordinary WhatsApp messages during an active request. `/status` and `/cancel` remain responsive during concurrent reader work. Do not add message steering or a user-request queue.
- [x] Final replies respect requested length and contain relevant uncertainty in ordinary language. No code-owned source-warning paragraphs are appended after the model's answer.
- [x] Remove superseded reader quote validation, mandatory fallback searches, forced chunk sweeps, coverage accounting, required findings schema and reader action/page counters. Optional oversized-content search/reading remains available without reviving mandatory scanning.
- [x] Remove obsolete configuration and tests for deleted policy, with clear handling of existing deployed configuration. Retain protections, execution limits and main web-search behavior that still apply.
- [x] Update the domain glossary, architectural decisions, runtime web instructions and operational documentation to describe the final implementation. Explicitly supersede conflicting earlier reader-design decisions without claiming deployment or successful live acceptance prematurely.
- [x] Run focused integration and regression checks covering main/reader coordination, screenshots, dynamic observations, overflow, follow-ups, progress, busy handling, cancellation and cleanup. Run the project's required broader checks before declaring the candidate ready for deployment.
- [x] Verify that existing vault-edit approval/sync behavior and main search are preserved. Vault-edit redesign remains outside this ticket even if it is a remaining latency bottleneck.
- [x] Record a reproducible predeployment verification result for the exact candidate, including reader timing/model-call instrumentation needed for live comparison. Do not activate the candidate as part of this ticket.

## Comments

2026-11-07 completion: normal web-skill assembly now exposes concurrent readers
and complete lifecycle tools. Retired reader/chunk/coverage/fallback/quote policy,
action/page allowances, structured Selenium capture and final-warning hooks are
removed. Configuration validation explicitly names retired keys to remove.
Progress follows observable work and preserves busy/status/cancel behavior;
transport generation checks prevent finals after cancellation/reset during
progress draining. Main search and vault approval/sync behavior are preserved.

Implementation: `85c51c6672c71204639b4345b2bd5a1214e74f3c`.
Reviewed source with cancellation-race fix:
`aaef0149fa531ac6008b16dc5a15eadb2541a5e5`.
Final full suite: **551 passed, 9 skipped in 469.28 seconds**. Focused typechecks,
lint, formatting and diff checks pass. Broader typecheck retains 24 existing
diagnostics, with no added messages against baseline. Standards review has no
findings; one Spec P1 was reproduced, fixed and independently confirmed resolved.
The wheel's 31 Python/skill files match the candidate; final resource inspection
found no Playwright workers. See [predeployment verification](../verification-03.md)
for commands, exact source, skips, wheel hash, trace instrumentation and limits.

No push, activation, live WhatsApp/Obsidian acceptance or performance-target claim
is made. Ticket 04 owns GitHub exact-SHA verification, Ubuntu provisioning,
staged installed-candidate checks, deployment and the measured live comparison.
