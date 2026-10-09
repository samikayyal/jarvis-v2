# 04 — Verify the packaged feature and document deployment

**What to build:** Deliver an installable, verified Selenium web-search feature
with clear browser prerequisites and an operational guide for enabling,
disabling, validating, and rolling it back. Demonstrate the finished request
flow in an isolated test environment and accurately separate that evidence
from later production acceptance.

**Blocked by:** 03 — Support moderate research and follow-up questions.

**Status:** complete

- [x] Verify a clean package install contains the web runtime skill, browser
  worker, tool integration, and consistent pinned Python dependencies and
  hash-locked deployment requirements. Use `uv` for dependency and execution
  commands. Complete any packaging fixes required for the installed artifact
  to reproduce the tested source behavior.
- [x] Document compatible Chromium/ChromeDriver provisioning and executable
  discovery for the native Ubuntu service account. Check startup under that
  account's intended constraints in the test environment, with the browser
  sandbox enabled. Runtime searches do not install packages/download drivers;
  missing or incompatible binaries produce a useful unavailable result.
- [x] Publish example settings for enablement, the agreed query/page/result
  defaults, excerpt bounds, and ordinary operation timeout. Explain validation,
  the normal restart requirement, and the separate existing overall request
  and tool-round ceilings. Do not reintroduce quick/moderate research timers or
  promise fixed response times.
- [x] Run the real browser against controlled HTML fixtures to demonstrate
  extraction, public-navigation restrictions, redirects, startup, profile
  isolation, and process-tree teardown. Test fixtures must not introduce a
  production bypass for local/private destinations. Focused tests in earlier
  tickets are prerequisites, not deferred work for this ticket.
- [x] With a compatible browser available, attempt a live Google query, a
  selected public page read, and a follow-up through the installed runtime's
  tool flow. Record browser/driver versions, outcomes, and evidence supporting
  the cited reply. A CAPTCHA or changed layout is a real observed failure,
  not a successful lookup; verify its fallback and state any missing live
  success evidence. Ordinary automated tests remain independent of Google.
- [x] Verify representative final scenarios: direct URL, quick result-only
  lookup, moderate comparison, partial evidence, genuine empty search,
  unavailable browser, CAPTCHA stop, exhausted counts, follow-up/recheck, and
  cancellation without orphan processes or late replies. Confirm strict zero
  additional search-service spending and separation from Google Workspace and
  OpenWA credentials/state.
- [x] Run relevant static checks and focused regressions as needed, followed
  by one complete suite at the end of the feature. Record exact results and
  unresolved environmental limitations; a timed-out or incomplete run is not a
  pass. Further runs require a change, failure, or unresolved concern.
- [x] The deployment guide covers browser installation/compatibility checks,
  configuration validation, controlled enablement/restart, disabling web search,
  and rollback while preserving existing assistant and messaging behavior.
  Document that configuration and compatible binaries alone do not prove a
  working Google search or a real WhatsApp reply.
- [x] Record delivery evidence and distinguish package/fixture/live-browser
  verification from production activation. Production host changes, enabling
  search there, and a real authorized WhatsApp search with a cited phone reply
  are later operational work, not silently performed by this ticket. Keep
  incomplete verification explicit and mark the ticket `complete` only when its
  required acceptance checks are satisfied.

## Notes

Follow the **Selenium web search** specification. Fix feature defects exposed by
verification narrowly, preserving unrelated checkout changes. If a required
browser/environment is unavailable, record the exact remaining check rather
than substituting a mock as proof. Do not modify OpenWA's deployment or reuse
its browser state to make a smoke test pass.

## Completion evidence — 2026-10-08

Delivered exact package/build dependency pins, a tracked and aligned `uv.lock`,
an installed-runtime probe, expanded real-browser tests, and the deployment
runbook. Verification exposed and fixed a proxy outcome bug: a blocked private
subresource/background connection must not discard a usable public document;
private main-document destinations and redirects remain blocked before access.

See [the full delivery record](../evidence/verification.md) for exact commands,
artifact checksum, source isolation, failed intermediate checks, test results,
scenario coverage, live JSON evidence, and limitations.

- Final complete suite: 492 passed, 3 POSIX-only skips in 337.78 seconds.
- Installed wheel on isolated Ubuntu: 118 web tests passed without skips.
- The skipped POSIX cases passed on Ubuntu; additional detached browser/crash
  handler checks passed (2 tests), and the terminal process test passed.
- Chromium and ChromeDriver 155.0.8059.39 ran as UID 10001 with umask 0077;
  Chromium reported namespace and Seccomp-BPF sandbox protection enabled.
- Live IANA read, cited scripted reply, transcript-based follow-up reuse, and
  explicit recheck succeeded through the installed runtime. Google was blocked
  by this environment's non-public DNS answer, with the required fallback notice.
  Successful live Google results remain unverified; this is not a successful
  Google lookup. Earlier timeout evidence is retained.
- Ruff, formatting, compileall, web type checks, dependency consistency, and
  separate Standards/Spec reviews passed. Probe review findings were fixed and
  covered by three targeted regressions.

This completes the ticket's isolated delivery/verification scope, including
honest recording of unsuccessful live Google attempts. Native production
systemd/AppArmor/egress acceptance, production enablement, and an authorized
real WhatsApp search with a cited phone reply remain separate operational work.
No production host, OpenWA state, Workspace credentials, or messaging behavior
was changed. The test provider made no paid model/search-service calls.

## Subsequent production acceptance — 2026-10-09

The user separately authorized continuation through the open WhatsApp chat.
Release `25ed2fb6286b8c9aaaa15c513f0e9d3ee42b0b93` is now active with web enabled.
Native full-suite validation passed (495 tests). The production service-account
probe exposed a native Chrome startup requirement, fixed with a fresh
request-owned HOME inside the private profile. Both review axes passed after a
provisioning documentation correction.

Real WhatsApp direct-page reading, citations, follow-up reuse, explicit reread,
and final idle status passed. Google returned `blocked`, and Jarvis disclosed
that limitation without inventing evidence. Successful Google search remains
unverified. See [production acceptance](../evidence/production-acceptance.md)
for exact release, rollback, binary versions, checks, and sanitized trace.

## Google JavaScript repair — 2026-10-10

The separately authorized repair is deployed as release `630811c`. Search now
permits scripts on the two Google HTTPS origins while ordinary reads remain
script-disabled, and specific search failure reasons reach the model. All 499
native tests passed; Standards and Spec reviews found no issues. The candidate
passed the JavaScript gate but encountered an unusual-traffic CAPTCHA. The real
new-release WhatsApp request timed out and its accurate timeout reply was recorded
as sent. Direct installed page reading and cleanup passed. Google success and
new-release final UI status remain unverified; see the
[repair acceptance record](../evidence/google-javascript-fix.md).
