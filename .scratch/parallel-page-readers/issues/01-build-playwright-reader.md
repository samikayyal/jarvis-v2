# 01: Build the Playwright reader end to end

**What to build:** Main Jarvis can start one page reader with a focused prompt and initial public URL, continue working, and collect its written findings. The reader investigates through persistent Playwright browser interactions, including pages too large to fit in its context. This establishes a testable replacement path without activating an incomplete production release.

**Blocked by:** None (can start immediately).

**Status:** claimed

- [ ] Start returns a reader ID immediately; status, result collection and waiting work through the main assistant's tool interface. A controlled scenario demonstrates that main Jarvis can do other work before the reader completes.
- [ ] Use asynchronous Python Playwright with an isolated browser context. Provision its dependency and browser requirements reproducibly using the project's uv workflow. Preserve public-address checks and existing private-network protections across initial loads, redirects and subsequent browser traffic.
- [ ] Supply full extracted readable initial content, preserving headings, tables and links without prior summary or relevance filtering. The reader receives only supplied task context and its own conversation, not private tools or automatic main-history access.
- [ ] Browser tools support inspect, click, type, select, scroll, back, forward, reload and tab list/switch/close. Links may cross domains, but no reader web-search tool, arbitrary-URL navigation tool, arbitrary-code interface or recursive delegation is exposed.
- [ ] Public website search/filter/calculator interactions work. Account login, private-account actions, purchases and resource creation remain outside reader authority; page content cannot expand that authority.
- [ ] Content-changing actions return updated readable content and element references. Browser tools handle normal loading waits; scrolling may return compact observations. Tabs and popups remain inside the reader context.
- [ ] A screenshot-request tool supplies images only when explicitly called. Verify that the configured reader model can receive and use them; unsupported capability fails clearly without silently switching models or claiming visual inspection.
- [ ] Initial reader configuration remains independently selected gpt-6-luna with low reasoning, 120 seconds per assignment, 24 model turns and configured context capacity with output reservation. Browser-operation timeouts and the earlier main-request deadline apply; no separate reader action/page allowances apply.
- [ ] Oversized initial or updated content is explicitly identified and remains fully available for optional local search and selected reading. No silent truncation, mandatory inspection order or exhaustive sweep is introduced.
- [ ] Context exhaustion returns an explicit limit with available findings, without silently dropping or summarizing history. Useful findings can be reported before exhaustion.
- [ ] The reader returns ordinary written findings with source URLs, optional useful excerpts and specific uncertainty. Its completion is not gated by quote validation, coverage accounting or a mandatory evidence schema.
- [ ] Action failures return actionable errors; uncertain clicks are not blindly replayed. Browser crashes disclose lost interactive state. Cancellation, timeouts and completion of the containing request close the single-reader resources.
- [ ] Repeatable real-browser fixtures verify dynamic selection, updated observations, cross-domain links, tabs, explicit screenshot delivery, oversized content access, limits and failure cleanup. A model-boundary test verifies screenshot input handling and plain-result delivery through main Jarvis.

## Scope notes

Use the approved concurrent browser readers specification as the design contract. Keep the existing main web-search implementation unless integration requires a change. Concurrent reader scheduling and cross-request retention are delivered in ticket 02; production-path cutover and deletion of the old machinery belong to ticket 03.

## Comments

2026-11-06: Implemented the opt-in single-reader path in `PlaywrightReaderTools`
and `PlaywrightBrowser`; default production assembly is unchanged. Review baseline:
`7857b88e2c23bfa1c1d76a832f6b155122d26d0a` (confirmed by the operator).
The agreed seams are the ticket's real-browser fixture boundary and main-tool/model
boundary. Focused integration/regression checks: 70 passed, 2 skipped. The separately
authorized live `gpt-6-luna` screenshot check passed in two turns, reading the
canvas-only code `ORCHID 7391`. Full-suite verification and the required two-axis
review remain before completion. See `docs/playwright-reader.md` for setup,
assembly, limits and verification scope. No deployment is part of this ticket.
