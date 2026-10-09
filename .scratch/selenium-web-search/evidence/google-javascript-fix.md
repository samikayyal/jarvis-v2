# Google JavaScript repair and deployment

Updated 2026-10-10 (Asia/Amman). This follows the user's explicit instruction:
“fix it and deploy then test”. Earlier operational evidence is retained in
[production acceptance](production-acceptance.md).

## Diagnosis and change

A service-account diagnostic using the deployed browser settings reached
`https://www.google.com/httpservice/retry/enablejs`. Google explicitly asked to
enable JavaScript. The public-address proxy had not blocked the request and no
reCAPTCHA was present. The reader globally disabled scripts; `search()` then
removed the specific reason from the returned outcome.

Local commit `e1f4fec` removes the global Chrome script-disable flag. Chrome's
default script policy remains blocked, with exceptions only for HTTPS
`www.google.com:443` and `consent.google.com:443`. CDP disables scripts before
clearing each document, enables them for a search, and disables them for ordinary
reads. Existing sandbox, anonymous profiles/HOME, public-address proxy, scheme
blocks, downloads, query/page limits, and challenge-stop behavior remain in place.
Search failures preserve a bounded reason (up to 512 characters, omitted when it
would exceed the total tool-output allowance).

The candidate diagnostic passed the JavaScript gate but reached Google's
`/sorry/index` page: “Our systems have detected unusual traffic from your computer
network.” reCAPTCHA was present, with no proxy rejection. This is failed search
evidence, not successful retrieval. No CAPTCHA was solved or bypassed.

## Validation and deployment

- Regressions were observed failing before changes: script-disable flag remained
  present and failure reasons were missing at both worker and tool boundaries.
- Focused Windows web checks: **82 passed, 1 POSIX-only skip**.
- Full native Ubuntu suite: **499 passed in 94.32 seconds**. Real-browser fixtures
  include a page script that would replace the extracted text if ordinary-read
  script disabling failed. Ruff and formatting passed.
- Standards review: no actionable findings. Spec review: no actionable findings;
  explicitly requires retaining unsuccessful Google-search evidence.
- Active production release: `630811c7ff65868049fd0b1c1c9e05e91daabe93`.
- Rollback release: `25ed2fb6286b8c9aaaa15c513f0e9d3ee42b0b93`.
- Built the wheel and installed hash-locked dependencies in the commit-named
  release. Installed-package probe and service-account CLI `--check` passed.
- Activation stopped the service, atomically changed `code/current`, started the
  service, and checked readiness; the transaction included automatic rollback.
  Config bytes were unchanged, including saved permissions. Config SHA256:
  `d835da06a7d66d3d186d86bcbb51931a5a50c4665f10f74f7cbd3f8de6df3ef2`.
- Rechecked live release, service `active`, restart count zero, exact named
  OpenWA session `ready`, gateway HTTP 200, web tool exposure and skill-aware
  prompt. Browser and driver remain 155.0.8059.39. OpenWA deployment was unchanged.

## WhatsApp acceptance on the new release

Sent one real request in the existing authorized Jarvis chat at 01:07 on
2026-10-10: search the public web once for IANA example domains and report the
specific failure reason if blocked, without terminal or private-data tools.

The runtime loaded `web` and attempted one quick `search_web`. The browser hit its
20-second operation timeout. The tool returned `outcome: unavailable`, reason
`browser operation timed out`, zero results, and zero remaining queries. It did
not retry. See [sanitized tool trace](google-fix-whatsapp-tools.json).

This production request did not reach a successful Google result. The earlier
candidate observation was an unusual-traffic challenge; these are separate
observations and neither should be reported as working Google search.

Browser control became unavailable after the request was sent. A fresh visible
reply screenshot, direct-page WhatsApp conversation, and final `/status` could
not be completed in this continuation. Earlier screenshots and idle status belong
to the previous release and are not reused as evidence for this one. Subsequent
server-side delivery/page/cleanup checks are recorded below.

Server-side follow-through:

- `openwa_send_result` confirmed `sent` at 2026-10-09T22:07:59Z. The reply said:
  “The web search was unavailable because the browser operation timed out. The
  tool did not report that Google blocked the request.” The standard unavailable
  evidence notice preceded it. No fabricated sources were returned.
- A service-account direct page read through the active installed `WebTools`
  returned `ok` for IANA's Example Domains page.
- Final checks: active release `630811c`, service active, `NRestarts=0`, web enabled,
  no service-owned Chrome/driver/worker processes, and zero temporary profiles.

The implementation fix and deployment are complete. Google search is still not
working reliably from this host: the candidate received a CAPTCHA, and the real
WhatsApp request timed out. New-release UI confirmation and final `/status`
remain unverified because browser controls were unavailable. Unrelated local
vault files and scratch work remain untouched.
