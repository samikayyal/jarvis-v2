# Ticket 02 implementation verification

Date: 2026-11-07 (Asia/Amman).

Scope: concurrent assignments and retained follow-ups on the opt-in
`PlaywrightReaderTools` path. Default production assembly and the deployed reader
remain unchanged. Production integration and Ubuntu activation belong to tickets
03–04; no deployment is part of this work.

Review baseline, confirmed by the operator:
`72cbb4f8372df4b49d67d7890c08793cd5429a8a`.
Initial implementation: `66a701864c77460febaa0696f6b940a3eba186cf`.
Reviewed source including deadline enforcement:
`eab346805ae2f0ceedd6106e47b2556b1fffaf97`.

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

Focused typechecking of both Playwright modules passes.
Checking `config.py` also reports a redundant-cast warning on an unchanged line;
no repository-wide typecheck pass is claimed. Config/skill regression checks:
110 passed. All 12 initial concurrency/lifecycle tests passed in the combined
browser/reader run. That run's only failure was an older test assuming one active
reader by default. It now explicitly configures `max_active_readers=1`; the
targeted capacity/cancellation regression passes. Repository lint/format and
`git diff --check` pass.

An additional real-browser regression first reproduced a provider returning a
late answer after swallowing deadline cancellation. The manager now records each
assignment's effective deadline and checks it before accepting results, returning
`limit/time_limit` and preserving earlier completed findings. The regression
passes. No provider requests or key access were needed.

Final full suite on reviewed source `eab3468`: **664 passed, 11 skipped in
560.60 seconds**. See [saved output](verification/full-suite-02.log). The skipped
checks remain existing opt-in/provider/platform fixtures; this task did not run a
billed model check or claim live WhatsApp acceptance. No source changes followed
this full-suite run; the completion commit only records documentation and output.

Final resource inspection after the suite found **0 Playwright Chromium/driver
worker processes** (Windows process inventory matched Playwright browser and
driver paths). The resource regression also observed all three context-close
events, driver exit codes and stopped proxy threads after active release/reset.
Final repository lint, formatting, focused typechecks and diff checks pass.

## Standards

Independent review of the full implementation against the confirmed baseline:
0 hard violations and 0 actionable judgment calls. The reviewed changes follow
issue-tracker conventions, domain vocabulary, ADR 0005 and the uv workflow, and
distinguish the opt-in path from production/deployment. The reviewer also checked
`66a7018...eab3468` and found no additional issue.

## Spec

Independent review found no additional mismatch across concurrency, independent
collection, busy/capacity errors, retained conversations, fresh budgets, explicit
release, idle reclamation, isolated contexts, cancellation/reset cleanup and
sequential main tools. The review acknowledged the parent's reproduced deadline
finding above, then checked `66a7018...eab3468` and confirmed it resolved: an
expired assignment cannot accept a late provider answer. No remaining actionable
Spec finding. Production integration and live acceptance remain tickets 03–04.
