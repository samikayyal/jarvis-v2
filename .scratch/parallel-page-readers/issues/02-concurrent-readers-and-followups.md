# 02: Add concurrent readers and retained follow-ups

**What to build:** Main Jarvis can investigate several sources concurrently, receive each reader's findings independently, follow up with retained readers, and stop work cleanly. Reader conversations and browser resources have separate explicit lifetimes.

**Blocked by:** 01 — Build the Playwright reader end to end.

**Status:** complete

- [x] Allow a configurable maximum of three active readers initially. Starts beyond capacity return a clear capacity result without creating hidden queued work. Only main Jarvis can spawn readers.
- [x] Demonstrate real overlap of browser-reader work and independent result delivery: a slow reader does not delay collection from a completed reader, and main Jarvis can use its own tools while readers run. Provide bounded waiting without a busy-polling loop.
- [x] Reader contexts isolate cookies, storage and tabs. Parallel reader work does not incidentally permit concurrent vault writes or other main-tool mutations.
- [x] A reader accepts only one active assignment. Follow-ups after completion reuse its conversation and surviving browser state; follow-ups while busy return a clear busy result without a queue.
- [x] Each follow-up has a fresh 120-second/24-turn assignment budget, still subject to the main-request deadline and available context in the retained conversation.
- [x] Initially retain up to eight reader conversations in the working session. Main Jarvis can explicitly release a reader. At capacity, require release rather than silently evicting conversation or findings; released IDs fail clearly.
- [x] Initially cap open reader browser contexts at three. Reclaim the least recently used idle context when a new context needs capacity, never an active context. Preserve conversation and findings, and explicitly disclose lost interactive state on reopening.
- [x] Main Jarvis can cancel an individual reader. Main-request completion and `/cancel` stop remaining reader work and close its browser contexts; completed findings survive in the working session. Cancelled work cannot send late answers.
- [x] A later request can follow up using retained findings and reopen a browser if necessary without claiming that interactive state survived. `/new`, inactivity reset, main-context reset and restart discard reader state and close resources.
- [x] Shared browser-process failure explicitly fails affected work and cleans up contexts; assignment state and active-capacity accounting cannot remain stuck after exceptions or cancellation.
- [x] Tests cover overlapping assignments, out-of-order completion, capacity rejection, follow-up busy behavior, context isolation, idle-context reclamation, explicit release, resets and cancellation during both model and browser work. Resource checks show no leftover contexts or worker processes after cleanup.

## Comments

2026-11-07 completion: implemented on the opt-in `PlaywrightReaderTools` path.
Implementation: `66a701864c77460febaa0696f6b940a3eba186cf`; reviewed source with
explicit deadline enforcement: `eab346805ae2f0ceedd6106e47b2556b1fffaf97`.
The operator confirmed the reader-manager, main runtime/tool and Playwright API
seams, and review baseline `72cbb4f8372df4b49d67d7890c08793cd5429a8a`.

Three active readers/contexts and eight retained conversations are configurable.
No starts or busy follow-ups queue work; explicit release frees conversation
capacity. Idle contexts use LRU reclamation; reopened readers disclose state loss.
Separate Chromium processes contain browser failure to one reader. Retained
follow-ups get fresh assignment budgets and preserve prior history/findings;
main deadlines and context capacity still apply. Existing runtime hooks handle
request completion, cancellation, session resets and restart; main tools remain
sequential.

Thirteen new real-browser/model-boundary tests cover the required concurrency and
lifecycle behavior, including providers returning after cancellation/deadline,
interrupted browser actions, runtime resets and actual resource teardown.
Final full suite: **664 passed, 11 skipped in 560.60 seconds**. Lint, formatting,
focused Playwright typechecks and both independent review axes pass, with no
remaining actionable findings. Final process inspection found zero Playwright
browser/driver workers. The configuration module's unchanged redundant-cast
warning remains outside the focused typecheck pass.

See [verification](../verification-02.md) and
[trial operations](../../../docs/playwright-reader.md). Production integration
and deployment remain tickets 03–04; no live cutover or acceptance is claimed.
