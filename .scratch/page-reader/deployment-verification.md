# Page reader implementation and acceptance

Date: 2026-11-04 (Asia/Amman)
Status: implementation checks in progress; deployment and WhatsApp acceptance pending.

## Scope

Work is isolated in `D:/Projects/Jarvis-v2-page-reader`, branch
`codex/page-reader`, based on `772e325`. Unrelated full-access and other local
changes in `D:/Projects/Jarvis-v2` remain untouched. The runtime uses the existing
provider adapter, supervised browser worker, tracing and session lifecycle.
No service, database, queue, persistent reader history or dependency was added.

## Verified so far

- Initial Windows runtime suite: 583 passed, 9 platform skips. Subsequent focused
  runtime/configuration/reader checks: 117 passed. Final suites are pending.
- Ubuntu real Chrome structured-capture fixture passed. It preserved headings,
  links and table cells, labelled US/EU selection, rejected cross-page navigation,
  excluded controls in forms and removed its temporary browser profile.
- Configured provider accepted `gpt-6-luna` with low reasoning. IANA read:
  19.43 seconds, one model attempt, 1,128 input / 230 output tokens, one capture.
  Initial JSON-mode rejection was corrected by adding the required JSON instruction
  to the input messages; model choice remained unchanged.
- GCP overview `https://cloud.google.com/compute/all-pricing` redirected to
  `https://cloud.google.com/products/compute/pricing`. It returned the official
  general-purpose link and source quotes excluding disk/images, networking,
  sole tenancy, Confidential VM and GPU pricing. 24.582 seconds, one attempt,
  7,049 input / 299 output tokens.
- The general-purpose page had seven captured chunks. The reader selected chunk
  4 and returned an exact E2 table quote: Iowa (us-central1), e2-standard-2,
  2 virtual CPUs, 8 GiB, Default (USD), $0.06701142 / 1 hour. It disclosed
  unverified billing conditions and separate disk/network charges. 51.633 seconds,
  two attempts, one chunk action, one capture, 9,708 input / 480 output tokens
  across calls. This is a point-in-time check, not a hard-coded price assumption.
- Source quote checks preserve original text, tolerating only whitespace
  differences; invented or rewritten quotes are removed with an explicit gap.
- Monetary cost is not estimated because an applicable price was not verified.

## Remaining gates

1. Final full Windows and Ubuntu suites, lint/format and source checks.
2. Commit/push candidate, verify its exact SHA on GitHub, build/install that source,
   and activate with only the explicit `[web_reader]` settings added to live config.
3. Visible WhatsApp GCP discovery/pricing, ordinary follow-up, explicit refresh,
   and cancellation through the in-app browser. End with idle `/status`, service
   health, ready OpenWA session and no browser workers/profiles.

At the latest retry, in-app browser-control tools were absent and the native
Computer Use helper returned `native pipe is unavailable` even after reset.
This does not count as WhatsApp acceptance. The operator has been notified.
