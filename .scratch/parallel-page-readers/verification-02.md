# Ticket 02 implementation verification

Date: 2026-11-07 (Asia/Amman).

Scope: concurrent assignments and retained follow-ups on the opt-in
`PlaywrightReaderTools` path. Default production assembly and the deployed reader
remain unchanged. Production integration and Ubuntu activation belong to tickets
03–04; no deployment is part of this work.

Review baseline, confirmed by the operator:
`72cbb4f8372df4b49d67d7890c08793cd5429a8a`.

Confirmed test seams: reader-manager prepared tools, main runtime/tool API, and
Playwright reader/browser API. Real Chromium exercises browser work; scripted
provider boundaries avoid billed API calls. Resource/fault checks inspect actual
browser context events and process/thread handles, without replacing internal
collaborators with mocks.

## Implementation

- Three active readers, three isolated browser contexts, eight conversations by
  default. All limits are positive integer configuration. Starts and busy
  follow-ups return explicit errors without queued work.
- Start/status/bounded wait/follow-up/cancel/release are main-only prepared tools.
  Main tool execution remains sequential. Real model-boundary gating demonstrates
  overlapping browsers and out-of-order collection while main tools run.
- Follow-ups retain conversation and live browser state, with a fresh assignment
  time/turn allowance and the earlier main deadline. Retained history still counts
  toward context capacity. Interrupted calls are sealed without replay.
- Idle browser contexts are reclaimed by least recent assignment use; active
  contexts are never reclaimed. Findings/conversations survive. Reopening and
  overflow observations disclose lost interactive state.
- Request completion, suspension, timeout and cancellation stop work and close
  browsers. Completed findings survive ordinary requests and `/cancel`; `/new`,
  inactivity/context resets and restart discard IDs and state.
- Each context has its own Chromium process, so no shared process failure can
  affect several readers. A disconnect fails the affected reader immediately,
  including while its model call is waiting, and frees capacity after cleanup.
- Cancellation marks work stopped before sending cancellation. A late provider
  result cannot overwrite findings. Real navigation cancellation is drained to
  avoid orphaned Playwright navigation futures.

## Validation

Validation in progress. Focused typechecking of both Playwright modules passes.
Checking `config.py` also reports a redundant-cast warning on an unchanged line;
no repository-wide typecheck pass is claimed. Config/skill regression checks:
110 passed. Combined browser/reader tests, full suite and both review axes will
be recorded before completion.
