# Implement page search, mandatory fallback and coverage reporting

Status: complete

Scope: local implementation and validation of the accepted
[specification](../spec.md). Local implementation is complete. Subsequent deployment
was authorized and completed; see [live acceptance](../deployment-verification.md)
and the [open feedback issue](02-live-reader-feedback.md).

## Acceptance

- [x] Local search returns matching chunk IDs only, with the accepted term limits and normalization.
- [x] Code requires bounded search, display review and remaining-chunk fallback for unresolved questions.
- [x] Retain one conversation; avoid duplicate chunk bodies and stale completion on follow-ups.
- [x] Validate supporting quotes and preserve source-specific coverage/stop facts and final disclosures.
- [x] Use 24 turns, 18 actions, 120 seconds and 128,000 context tokens by default.
- [x] Preserve snapshot/display labels, refresh behavior and failed-freshness disclosures.
- [x] Real local Chrome fixture reveals hidden billing content and returns a validated quote.
- [x] Complete relevant regression checks and record results.

## Comments

The operator's final correction reinstated Q17 after an earlier preference for
agent-directed investigation. The consolidated specification and ADR 0004 reflect
the final choice. Scripted model fixtures validate runtime mechanics, not semantic
accuracy of a live model. The local Chrome fixture exercises SeleniumPageReader
and page controls; Linux worker-process isolation is covered separately by the
existing POSIX-only suite.

Verification complete: 631 passed / 10 skipped in the full suite; 125 passed in
the final focused reader/configuration run; 1 passed in the opt-in real Chrome
fixture. Ruff, formatting and diff whitespace checks passed. See
[verification](../verification.md) for commands and limitations.
