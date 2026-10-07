# 02 — Search Google and answer quick lookups

**What to build:** Jarvis performs a quick Google search when the authorized
operator asks or when freshness/verification matters, optionally reads selected
source pages, and returns a cited web-assisted answer. Stable questions can
still be answered without searching.

**Blocked by:** 01 — Read public pages and return a cited answer.

**Status:** complete

- [x] Add `search_web` to the existing web runtime skill. It accepts one
  nonempty query and depth, and uses ordinary Selenium operations in the
  request-owned browser to return organic titles, URLs, snippets, query outcome,
  and remaining allowances. Continue to expose only completed quick behavior
  until ticket 03 adds moderate research.
- [x] Enforce the configured quick query ceiling across every search call in
  the ordinary request, defaulting to one query and five organic results.
  Count failed network attempts too. Searching consumes query allowance;
  reading selected destinations consumes the same page allowance established
  by ticket 01. No repeated call resets either counter.
- [x] Extract organic results deterministically and exclude advertisements and
  Google's synthesized answer panels. Preserve usable destination URLs and
  source metadata; count and text bounds apply before returning valid JSON.
  Normalization or Google redirect handling cannot bypass the public-navigation
  restrictions.
- [x] Distinguish successful results, genuine `no_results`, `blocked`,
  `extraction_failed`, `unavailable`, and `limit_reached`. An unknown result
  layout must not masquerade as a successful empty search. Recognized consent
  screens may reject optional cookies; unrecognized interstitials are blocked.
- [x] Detect CAPTCHA/search blocking, stop further Google searches for the
  request, and preserve evidence already returned. Do not wait for operator
  intervention, automate challenge solving, silently retry, or use paid search,
  model, proxy, or browser services. Existing OpenAI usage remains separate.
- [x] The model automatically loads the web skill for explicit search requests
  and relevant freshness/verification needs, chooses a concise query in an
  appropriate language, and stops when evidence is sufficient. Send only the
  question and minimum relevant context. Private vault/email/calendar content
  requires explicit operator intent; do not send the complete conversation or
  use the Google Workspace connection for browser search.
- [x] Quick answers can use clear result snippets or selected page reads.
  Evidence identifies which sources were actually read versus only seen in
  results. The answer cites returned sources with titles/URLs and never claims
  to have opened an unread source. Prefer relevant primary sources.
- [x] Extend the failure-notice behavior from ticket 01: no usable evidence
  after access fails requires explicit web-unavailable disclosure; a genuine
  empty search says no useful results were found. Useful partial evidence and
  count-limit outcomes retain their distinct meanings. The model does not
  invent fresh facts when it must fall back to existing knowledge.
- [x] Focused fixture and model-and-tool-loop tests cover normal results,
  ads/answer-panel exclusion, empty results, changed layouts, consent, CAPTCHA,
  query ceilings, snippet-versus-page citations, privacy instructions, fallback,
  and search followed by a bounded page batch. Ordinary tests require no live
  Google connection. Preserve ticket 01's process cleanup and cancellation
  guarantees.

## Notes

Follow the **Selenium web search** specification. The demo is an ordinary
freshness-sensitive request that loads the skill, searches, optionally reads
sources, and produces a cited reply. Use `uv`, preserve unrelated work, and run
focused checks. Keep the existing tool-round and overall request limits; no
research-phase timers or additional model service. Production activation is
outside this ticket.

## Completion evidence — 2026-10-07

Implemented quick `search_web` through the existing request-owned Selenium
worker. Organic results preserve source identity and search-result provenance;
selected reads retain page provenance and share cumulative query/page/evidence
allowances. Failed attempts count, Google blocking stops further searches, and
empty results, extraction failures, access failures, and limits stay distinct.
The packaged skill covers automatic loading, concise queries, private-context
intent, primary sources, citations, and conservative fallback.

Validation: final focused package and web tests passed (104 passed, 1 skipped).
Ruff lint/format, compilation, and `ty` checks on the five web/skill modules passed.
The wheel built with the search module, packaged skill, and Beautiful Soup
metadata; the deployment hash lock resolves successfully in a dry-run install.
The repository ignores its local uv.lock; deployment requirements.in and the
tracked hash lock include the new parser dependency without upgrading other pins.

The full suite was run once: 468 passed, 2 skipped, 1 failed in 346.88 seconds.
Its sole failure was the package test's exact dependency list, which still
omitted the new parser. That assertion and deployment pins were updated; the
final 104-test focused pass includes all package tests and the complete web path.
The complete suite was not repeated after that correction. The skips require
configured real-browser or platform-specific facilities.

Parallel Standards and Spec reviews completed. Repairs consolidated browser
creation and JSON fitting and added regressions for false blocking from ordinary
snippets or hidden dialogs, false empty-result detection in unknown layouts,
and a small-output failure overflow. All identified findings were addressed.
Scripted model/tool-loop tests validate supplied instructions and evidence flow;
no live model or Google acceptance is claimed. Chromium/ChromeDriver provisioning,
Ubuntu service-account verification, moderate research, and production activation
remain outside this ticket. Unrelated vault changes were preserved.