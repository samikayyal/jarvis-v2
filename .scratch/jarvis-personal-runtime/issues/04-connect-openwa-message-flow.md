Type: task
Status: complete
Blocked by: 03

## Question

Implement the minimal OpenWA inbound and outbound path: acknowledge the private webhook promptly, validate the agreed basic direct-text shape and authorized sender, suppress duplicates and excluded traffic, route admitted text into the runtime, and send deterministic or model-produced replies through the preserved OpenWA API with deterministic chunk ordering.

Focused tests must cover admission, immediate acknowledgement, duplicate suppression, ignored traffic, one send attempt per chunk, send uncertainty, and no change to the pinned OpenWA gateway contract.

## Answer

Implemented a fresh replacement-owned OpenWA message flow without importing the
legacy control plane. Signed `message.received` direct-text events for the
configured internal session and authorized operator are acknowledged with 202
before runtime work begins; unauthorized, group, self-authored, media, malformed,
and duplicate traffic cannot enter assistant work.

Runtime replies are split deterministically at safe text boundaries and sent in
order through the independently verified `messages/send-text` route. Every chunk
has one attempt only; a possibly-sent failure is reported as unknown, is never
retried, and stops later chunks. Blocking gateway HTTP runs outside the event
loop so one stalled send cannot delay the next webhook acknowledgement.

The replacement configuration now carries the OpenWA API base URL, internal
session ID, distinct human-readable session name, authorized operator number,
and operator chat ID in `jarvis.toml`, while API and webhook signing credentials
remain in `.env`.

## Comments

- Completed on 2026-08-30 with 59 focused replacement tests and all 11 retained
  Ticket 13 OpenWA contract tests passing; Ruff, formatting, and bytecode
  compilation were clean.
- The required two-axis review found no remaining actionable spec defect or hard
  standards violation after fixes.
- The one final full-suite run completed with 847 passed and 2 skipped. Its only
  failures were the four expected legacy deployment-pin checks because the fresh
  replacement source intentionally differs from the immutable legacy
  `artifacts.lock.json`; that rollback pin was not weakened or repinned.
