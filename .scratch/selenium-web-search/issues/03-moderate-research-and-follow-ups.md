# 03 — Support moderate research and follow-up questions

**What to build:** Jarvis can compare public sources, expand a quick lookup into
moderate research when needed, and answer follow-up questions using the current
working session's evidence. Every new search or page read stays within the
configured count limits.

**Blocked by:** 02 — Search Google and answer quick lookups.

**Status:** complete

- [x] Both web tools support `quick` and `moderate` depth. The model selects
  depth according to the request, with configurable default ceilings of one
  query/two page attempts for quick and three queries/five page attempts for
  moderate. Results per query and evidence bounds remain configurable through
  `jarvis.toml`. Valid changes take effect on the normal validated restart.
- [x] The first web call selects depth. A subsequent moderate call can upgrade
  a quick request once; consumed query/page/evidence allowance carries forward.
  Later quick calls cannot downgrade or reset the budget. A used quick query
  leaves two additional queries under the default moderate ceiling, including
  when the first attempt failed.
- [x] Enforce shared totals across repeated searches, page batches, failures,
  direct links, and a resumed approval continuation. Duplicate network attempts
  still count; reuse of already returned evidence without network work does not.
  Batch overflow reports unattempted URLs. Counters reset for a new ordinary
  request, not for another tool call or skill load.
- [x] Stop early when evidence suffices, or return the available evidence when
  a query/page/output cap is reached. Limits are ceilings, not targets. Retain
  the existing maximum tool rounds and ordinary request timeout; add no separate
  research clock, timed upgrade condition, parallel browser pool, or hidden
  automatic retries.
- [x] Moderate answers compare relevant sources, prefer primary sources, and
  explain material disagreements and meaningful coverage gaps. Each supported
  claim uses source citations from returned evidence. A partial batch, blocked
  search after earlier success, or exhausted count allowance does not erase
  useful evidence or become a blanket outage notice.
- [x] Evidence and loaded skills persist only through the existing working
  session behavior. Follow-ups may reuse that evidence; time-sensitive claims
  and explicit recheck requests trigger fresh tool work. Session expiry, `/new`,
  context reset, or restart does not restore old evidence from a new cache.
  Each ordinary request still owns and closes its own browser.
- [x] Normal runtime tracing records bounded tool arguments/results. There is
  no separate persistent search cache or retained browser profile, no full-page
  HTML/cookie/screenshot trace payload, and no new authority granted by web text.
- [x] Focused end-to-end tests exercise a quick-to-moderate upgrade without
  resets, moderate-first research, attempted downgrades, cumulative batches and
  failed attempts, partial evidence, direct URLs, approval continuation, and
  output limits. Include a two-message conversation demonstrating reuse and an
  explicit recheck demonstrating new tool work, plus session-reset and cancel
  cases with no late reply.

## Notes

Follow the **Selenium web search** specification and extend the working tools
from tickets 01 and 02 rather than introducing another orchestration layer.
The demo is a question needing multiple sources, a useful partial answer when
one source fails, and a follow-up/recheck with correct budget lifetime. Use
`uv`, preserve unrelated work, and run focused checks; ticket 04 owns the final
complete-suite run. Production activation is outside this ticket.

## Completion evidence — 2026-10-07

Both web tools now expose quick and moderate depth. Selecting moderate changes
the active request's configured ceilings without resetting query, page, or
evidence usage; later quick calls cannot downgrade them. New ordinary requests
reset the allowance. The existing request/browser lifecycle and working-session
transcript provide approval continuation, follow-up reuse, and reset behavior.
The packaged web skill now guides source comparisons, material disagreements,
coverage gaps, and fresh work for explicit rechecks and time-sensitive claims.
Deployment and domain documentation describe the completed behavior.

Deterministic model/tool-loop tests demonstrate moderate-first research and an
upgrade after successful or failed quick work, repeated skill loading, failed
and duplicate page attempts, direct links, unattempted batch overflow, preserved
partial evidence, approval suspension/resumption, bounded JSON and trace output,
follow-up reuse without a browser, and rechecks with fresh request allowances.
Session expiry, `/new`, context reset, restart, and cancellation are covered,
including browser cleanup and no late research reply.

Validation:

- Web-tool and Responses tests: 75 passed.
- Focused web/configuration/skill/runtime tests: 226 passed, 1 skipped.
- Full suite, run once as required by the invoked implementation skill:
  488 passed, 2 skipped in 280.73 seconds.
- Repository runtime/test Ruff lint and format checks passed; `ty` checks for
  the changed web adapter and skill adapter passed; `git diff --check` passed.
- Separate Standards and Spec reviews reported zero findings. The Spec review
  independently reran the 75 web-tool/Responses tests successfully.

The skips require configured compatible Chromium/ChromeDriver paths and a POSIX
process-group environment. Scripted model and browser boundaries verify evidence
flow and supplied instructions, not live-model answer quality or live Google
acceptance. Ticket 04 still owns final package/deployment verification and
host/browser readiness. Production activation was not performed. Unrelated
vault edits and scratch work were preserved.
