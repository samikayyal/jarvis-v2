# Page reader implementation and acceptance

Date: 2026-11-05 (Asia/Amman)
Status: complete; implemented, deployed, and visibly accepted through WhatsApp in Chrome.

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

## Visible WhatsApp acceptance

On 2026-11-05 the operator explicitly switched from the unavailable in-app
browser to Chrome ("use chrome bro"). Tests used the existing Jarvis WhatsApp
chat, with read-only public-web prompts, from 04:10 to 04:16 Asia/Amman.

- Discovery searched first, read the official Compute Engine overview, followed
  its returned General-purpose pricing link, and selected chunks 4 and 7 of 7.
  The visible answer verified Iowa (us-central1), e2-standard-2, 2 vCPUs, 8 GiB,
  Default/on-demand USD $0.06701142/hour. It included a source excerpt, both
  official links, separate-cost exclusions from the overview, and gaps in
  purchase conditions and total running cost. Search returned unhelpful
  third-party snippets; Jarvis used the official overview directly instead.
- Reader `7777e2451b0f` owned the overview; `aeb666468bdd` owned General-purpose
  pricing. Raw request inspection confirmed low-reasoning reader requests had
  only `read_page_chunk` and `display_page`, isolated from the main assistant's
  medium-reasoning conversation/tools. Full selected chunks stayed in reader
  input; the main transcript received compact findings and IDs.
- The explicit evidence-only follow-up used `aeb666468bdd`, refresh=false,
  actions=0, page_work=0. The visible reply explained default versus CUD columns
  and disclosed unverified inclusions/terms.
- Explicit refresh used the same ID, page_work=1, and new captured evidence.
  The first refreshed model request contained only three initial input items
  and no previous chunk exchanges; inspection reset to chunk 4 of 7. The visible
  reply quoted the current table labels and row. The price was unchanged.
- A new reader began capture at 01:15:33Z, then `/cancel` interrupted it.
  Its trace outcome was `cancelled` after 12.039 seconds, with no inference
  attempts and no inspected chunks. WhatsApp confirmed cancellation. No late
  comparison answer appeared during subsequent follow-up and final status.
- The earlier completed reader survived cancellation. An ordinary follow-up
  inspected previously unread chunk 7 of the refreshed capture, retained chunk
  4, and did zero page work. Its visible answer correctly restricted the
  one-minute billing statement to RHEL images rather than VM compute and
  disclosed remaining unread chunks.
- Final visible `/status` at 04:16: active request none; pending action no;
  main model gpt-6-luna (medium); context about 26,695/250,000 tokens.
  Post-test health confirmed the exact active release above, active/running
  service, NRestarts=0, zero browser processes and zero temporary profiles.
  OpenWA returned HTTP 200, exact named session ready, identity matching.
  Prompt/config hashes still matched the recorded deployment values.

### Reader latency and provider usage

Input/output counts below sum model calls within each invocation, including
cached input and repeated retained context; they are not peak context sizes.
All completed invocations returned `ok`, with zero transient retries.

| Invocation | Seconds | Model calls | Actions | Page work | Input tokens | Output tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Overview | 21.394 | 1 | 0 | 1 | 7,119 | 448 |
| General-purpose pricing | 53.105 | 3 | 2 | 1 | 24,270 | 683 |
| Jarvis's automatic conditions follow-up | 11.341 | 3 | 2 | 0 | 65,676 | 893 |
| Explicit evidence-only follow-up | 4.762 | 1 | 0 | 0 | 29,229 | 386 |
| Explicit refresh | 43.668 | 2 | 1 | 1 | 9,802 | 323 |
| Follow-up after cancellation | 7.600 | 2 | 1 | 0 | 23,672 | 507 |

The automatic conditions follow-up reread selected chunks locally, increasing
retained context and token usage without refetching. This is permitted by the
bounded contract; no optimization framework was added. General compute billing
minimums and full commitment terms remain source-coverage gaps, not verified
claims. Cost is not estimated without applicable verified model pricing.

Local evidence: `whatsapp-trace.log` and `whatsapp-audit.log` under this scratch
directory (ignored evidence), plus pricing and final-status screenshots in the
chat's visualization directory. These
logs contain only acceptance-window selected events and request-shape summaries,
not credentials. Screenshot evidence remains local and is not pushed to GitHub.
