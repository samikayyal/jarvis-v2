# Page reader implementation and acceptance

Date: 2026-11-04 (Asia/Amman)
Status: implemented and deployed; visible in-app WhatsApp acceptance pending.

## Scope

Work is isolated in `D:/Projects/Jarvis-v2-page-reader`, branch
`codex/page-reader`, based on `772e325`. Unrelated full-access and other local
changes in `D:/Projects/Jarvis-v2` remain untouched. The runtime uses the existing
provider adapter, supervised browser worker, tracing and session lifecycle.
No service, database, queue, persistent reader history or dependency was added.

## Verified so far

- Final Windows runtime suite: **597 passed, 9 platform skips**, 236.36 seconds.
- Final Ubuntu runtime suite: **606 passed, no skips**, 239.44 seconds, including
  real headed/headless browser, challenge/network restrictions and cleanup tests.
- Ruff check and format checks passed on both hosts. All **62** runtime/test files
  from the Ubuntu run match the verified release source after newline normalization.
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

## Candidate and activation

- Candidate **`f273314789764641de5b8fd9ab15775b54400b80`** was committed and pushed
  to `origin/codex/page-reader`. `git ls-remote` verified the exact GitHub SHA
  before staging and again before activation.
- The Git bundle, source checkout, release archive, wheel and non-editable install
  came from that candidate. Every installed runtime source/skill file was compared
  byte-for-byte with the release archive before changing the active symlink.
- Active directory:
  `/home/samik/Projects/Jarvis/code/releases/f273314789764641de5b8fd9ab15775b54400b80`.
- Rollback retained: `f8f78112d5e1c98473be588709e1bd86c6d7f412`.
- Live config changed only by appending `[web_reader]`, `model = "gpt-6-luna"`,
  `reasoning_effort = "low"`. Original config is retained beside it as
  `jarvis.before-page-reader.toml`. Existing browser timeout is 60 seconds; shared
  quick/moderate page limits and evidence limits remain unchanged.
- Config SHA256: before
  `a853ddeccf4e603dd2ee8b2d35fac78503394f1f3b21d4a1cdaa46c361515d75`, after
  `9d76db623deef741509923396b36e46c67d5b4ab94ffa8a07a105ebb94f90c59`.
- Prompt unchanged, SHA256
  `33ca22c6f7c7b5b162bd29014139b10ee5b192fec3395136d6bac1672306ba0a`.
  The older readiness helper's historical-example mismatch is unrelated; actual
  pre/post prompt bytes were checked directly.
- Service-account startup check passed. Installed web skill exposes `search_web`,
  `read_web_page`, `ask_page_reader`, with no `read_web_pages` bypass.
- Post-activation: service active/running, NRestarts=0; OpenWA HTTP 200 and exact
  named session ready; zero browser worker processes and zero temporary profiles.

## Remaining acceptance gate

Visible WhatsApp GCP discovery/pricing, ordinary follow-up, explicit refresh,
and cancellation through the in-app browser. End with idle `/status`, service
health, ready OpenWA session and no browser workers/profiles.

At the latest retry, in-app browser-control tools were absent and the native
Computer Use helper returned `native pipe is unavailable` even after reset.
This does not count as WhatsApp acceptance. The operator has been notified.
