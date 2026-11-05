# Implement and accept the conversational page reader

Status: complete

Implement the [accepted spec](../spec.md) with the existing runtime, provider
adapter, public browser worker, tracing and working-session lifecycle.

Completion requires fixture coverage, applicable full checks, real provider
validation, exact GitHub candidate verification before Ubuntu activation, and
GCP discovery/pricing, evidence-only follow-up, explicit refresh and cancellation
through WhatsApp in the authorized browser. The operator switched acceptance
from the in-app browser to Chrome on 2026-11-05. Finish idle with clean browser resources.

## Comments

- 2026-11-04: Implementation isolated on `codex/page-reader` in
  `D:/Projects/Jarvis-v2-page-reader`; unrelated changes in the original checkout
  remain untouched. Windows initial suite: 583 passed, 9 platform skips.
  Ubuntu real structured-capture fixture and configured-provider IANA check passed.
  Deployment and visible WhatsApp acceptance remain pending.
- 2026-11-04: Candidate `f273314789764641de5b8fd9ab15775b54400b80` pushed,
  GitHub-verified and activated. Final suites: Windows 597 passed/9 skips;
  Ubuntu 606 passed/no skips. See [verification](../deployment-verification.md).
  Only visible in-app WhatsApp acceptance remains open; browser automation
  is currently unavailable in the chat.
- 2026-11-05: Completed visible WhatsApp acceptance in Chrome, explicitly
  authorized by the operator. GCP overview-to-E2 pricing, evidence-only reuse,
  same-ID refresh, active cancellation, and unread-chunk follow-up after
  cancellation passed. Final `/status` is idle with no pending action;
  service/OpenWA healthy and zero browser workers/profiles. See the final
  [verification record](../deployment-verification.md).
