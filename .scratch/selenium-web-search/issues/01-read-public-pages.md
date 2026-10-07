# 01 — Read public pages and return a cited answer

**What to build:** The authorized operator can send Jarvis a public HTML link
and receive a web-assisted answer with source citations. Deliver the complete
quick page-reading path through the existing model-and-tool loop, a packaged
web runtime skill, and one request-owned Selenium browser.

**Blocked by:** None — can start immediately.

**Status:** complete

- [x] An enabled web runtime skill exposes `read_web_pages` after normal skill
  loading. It accepts a nonempty bounded list of public HTTP(S) URLs and depth,
  reads pages sequentially, and returns each page's title, final URL, bounded
  visible text, retrieval outcome, and remaining allowance. This slice exposes
  quick reading only; it does not advertise unimplemented search or moderate
  behavior.
- [x] Non-secret web settings are validated through `jarvis.toml` at startup
  and applied through the existing restart flow. An absent section or disabled
  setting leaves web access off. Establish the agreed defaults: quick one
  query/two pages, moderate three queries/five pages, five results per query,
  8,000 characters per page, 32,000 total evidence characters, and a 20-second
  browser-operation timeout. Validate positive finite timeouts, positive
  integer counts/character limits, quick limits no larger than moderate limits,
  and total evidence capacity at least as large as the per-page allowance.
- [x] Quick page reads share a cumulative per-request allowance, including
  across tool calls. Count attempted reads when they start, including failures;
  reject malformed or disallowed inputs before navigation. Read only the
  allowed prefix of an oversized batch and report the rest as unattempted.
  Direct links consume page allowance without consuming query allowance.
- [x] Start one headless Chromium browser lazily on the Ubuntu runtime host,
  with its sandbox enabled and a fresh anonymous profile in the private runtime
  directory. A separately owned Python process performs Selenium work without
  blocking the event loop. Browser execution does not use the terminal tool,
  inherit personal or Google Workspace credentials, or touch OpenWA state.
- [x] Enforce public HTML scope during navigation, including scheme/address
  validation and redirects; checking only the final URL is insufficient.
  Private/internal destinations and local files are inaccessible through this
  tool. PDFs, downloads, authenticated pages, and general browser actions are
  unsupported. Test the actual browser restriction mechanism.
- [x] Return valid structured JSON within both web evidence and runtime output
  bounds, preserving source identity when shortening text. Report partial
  batches, blocked/unsupported reads, extraction failures, unavailability, and
  exhausted allowance honestly. Do not return HTML, cookies, profiles, or
  screenshots as evidence; do not silently retry failed reads.
- [x] The existing model produces a naturally sized answer with numbered
  citations and source titles/URLs. Web text is untrusted evidence, cannot grant
  approval or instruct other tools, and cannot cause private context to be
  transmitted. With no usable evidence after access fails, the runtime ensures
  an unavailable-web notice accompanies the knowledge-based answer. Partial
  evidence remains usable with meaningful gaps disclosed; reaching a count
  limit is not an outage. Current facts are not fabricated when verification
  fails.
- [x] Browser startup and individual operations are bounded by the ordinary
  operation timeout and existing overall request cancellation. There are no
  quick/moderate research timers. On a page timeout, retain previous evidence
  and proceed only while the worker remains usable; a browser crash ends that
  worker without a hidden restart/retry.
- [x] Reuse the browser only within the active request and terminate its process
  group and remove its temporary profile on completion, error, timeout,
  cancellation, or shutdown. Clean up when a request suspends for approval,
  while retaining consumed counters for its continuation. A fresh ordinary
  request resets counters; skill loading/session reuse does not reset an active
  request. `/cancel` must not leave children running or produce a late reply.
- [x] The installable package contains its new skill and Python dependency.
  Keep package metadata, deployment requirements, and the hash-locked
  dependency set consistent. Browser/driver binaries are explicit prerequisites;
  the runtime never installs packages or downloads drivers, and missing or
  incompatible binaries yield a clear unavailable outcome.
- [x] Focused configuration, skill, runtime, package, and browser-worker tests
  verify direct-link answers, count enforcement, output bounds, partial reads,
  required failure disclosure, navigation restrictions, request reset, approval
  continuation, and timeout/cancel/process-tree cleanup. Use deterministic page
  fixtures and controllable processes; include real-browser fixture checks for
  the implemented extraction and restrictions, recording environment limits.

## Notes

Use the agreed **Selenium web search** specification in this feature as the
source of the final contract. Keep this slice demoable with a direct public
link; tickets 02 and 03 extend that working path. Use `uv` for all Python work,
preserve existing runtime-skill and vault changes, and keep refactoring narrow.
Run focused checks here; the final complete-suite run belongs to ticket 04.
Production activation is outside this ticket.

## Implementation and verification — 2026-10-07

Implemented `WebSearchConfig`, the packaged quick-only `web` skill, request-owned
`WebTools`, and a separate Selenium worker with a public-address proxy. Direct
links consume the cumulative page allowance. Evidence is bounded valid JSON;
the model receives citation and untrusted-evidence instructions, and the runtime
adds unavailable/partial notices. Browser cleanup is cancellation-resistant,
including repeated cancellation, and approval continuation retains counters.

Focused checks: 179 configuration, package, skill, Responses, runtime, service,
and web-tool tests passed; 20 browser tests passed with the real-browser fixture
enabled. The latter used Chrome 154.0.8037.93 and an explicitly provisioned
temporary ChromeDriver 154.0.8037.92 on this Windows development machine. It
verified visible text extraction, suppression of page JavaScript, a private
redirect receiving no request, and denied downloads returning no stale evidence.
The fixture-only DNS substitution is test code, not a production bypass.
Process-group signalling after a leader exits and repeated-cancellation cleanup
are covered with controllable process boundaries. No browser children from this
fixture run remained in the local process inventory.

Ruff check/format, compileall, dependency lock validation, and wheel resource/
Selenium metadata checks passed. `ty` passes on the four new/extended web and
skill modules. A clean temporary environment installed the hash-locked deployment
requirements and built wheel successfully, then imported the web worker, tools,
and packaged skill with Selenium 4.50.0. Checking the older Responses module
also surfaces existing typing diagnostics, so no repository-wide typecheck pass
is claimed.

The two-axis review found and repaired repeated-cancellation cleanup and answer
notice overflow, plus removed a redundant worker wrapper. Page JavaScript is
disabled, so sites requiring it can yield incomplete or unavailable evidence.
Ubuntu service-account sandbox/startup and live WhatsApp acceptance remain
unverified operational prerequisites. No production service or configuration
was activated. The complete-suite run remains assigned to ticket 04.
