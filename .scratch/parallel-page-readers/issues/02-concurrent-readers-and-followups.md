# 02: Add concurrent readers and retained follow-ups

**What to build:** Main Jarvis can investigate several sources concurrently, receive each reader's findings independently, follow up with retained readers, and stop work cleanly. Reader conversations and browser resources have separate explicit lifetimes.

**Blocked by:** 01 — Build the Playwright reader end to end.

**Status:** ready-for-agent

- [ ] Allow a configurable maximum of three active readers initially. Starts beyond capacity return a clear capacity result without creating hidden queued work. Only main Jarvis can spawn readers.
- [ ] Demonstrate real overlap of browser-reader work and independent result delivery: a slow reader does not delay collection from a completed reader, and main Jarvis can use its own tools while readers run. Provide bounded waiting without a busy-polling loop.
- [ ] Reader contexts isolate cookies, storage and tabs. Parallel reader work does not incidentally permit concurrent vault writes or other main-tool mutations.
- [ ] A reader accepts only one active assignment. Follow-ups after completion reuse its conversation and surviving browser state; follow-ups while busy return a clear busy result without a queue.
- [ ] Each follow-up has a fresh 120-second/24-turn assignment budget, still subject to the main-request deadline and available context in the retained conversation.
- [ ] Initially retain up to eight reader conversations in the working session. Main Jarvis can explicitly release a reader. At capacity, require release rather than silently evicting conversation or findings; released IDs fail clearly.
- [ ] Initially cap open reader browser contexts at three. Reclaim the least recently used idle context when a new context needs capacity, never an active context. Preserve conversation and findings, and explicitly disclose lost interactive state on reopening.
- [ ] Main Jarvis can cancel an individual reader. Main-request completion and `/cancel` stop remaining reader work and close its browser contexts; completed findings survive in the working session. Cancelled work cannot send late answers.
- [ ] A later request can follow up using retained findings and reopen a browser if necessary without claiming that interactive state survived. `/new`, inactivity reset, main-context reset and restart discard reader state and close resources.
- [ ] Shared browser-process failure explicitly fails affected work and cleans up contexts; assignment state and active-capacity accounting cannot remain stuck after exceptions or cancellation.
- [ ] Tests cover overlapping assignments, out-of-order completion, capacity rejection, follow-up busy behavior, context isolation, idle-context reclamation, explicit release, resets and cancellation during both model and browser work. Resource checks show no leftover contexts or worker processes after cleanup.
