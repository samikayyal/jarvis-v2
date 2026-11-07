---
status: accepted
---

# Simplify and parallelize page readers

The operator selected a replacement direction on 2026-11-06: main Jarvis delegates
concurrent public-web investigations using a prompt and an initial webpage;
readers receive full extracted readable page content, may navigate beyond that
page, and decide when their findings are sufficient. Remove code-enforced quote
validation and mandatory fallback scanning, trading deterministic quote-occurrence
and investigation checks for simpler orchestration and model-led investigation.

Public research interactions are allowed; account login, private-account actions,
purchases and resource creation are outside reader authority. Findings and
uncertainty remain responsibilities of the reader and main assistant; removing
validation does not establish correctness or authorize fabricated evidence.

Reader starts return immediately; main Jarvis can continue work and collect
results independently. Only main Jarvis delegates. Readers use general browser
action tools with persistent interactive state during the active request, then
their browsers close when main Jarvis finishes. Reader conversations survive in
the working session; a later browser reopen does not preserve interactive state.
Oversized content is explicitly identified and remains available for optional
search or selected reading. Findings are written reports with source URLs,
optional supporting excerpts and specific uncertainty, without a mandatory
evidence schema or automatic WhatsApp warning appendices.

Start with a configurable maximum of three active readers and explicit capacity
failure instead of a queue. Readers receive no web-search tool; their browser
interactions begin at the supplied webpage. They may follow links across domains,
but have no arbitrary-URL navigation tool. Screenshots require an explicit tool request. Main-request
completion or cancellation stops remaining reader work, with no late replies.
Existing busy-message behavior remains; lengthy work gains brief progress after
roughly 20 seconds and further updates only for meaningful developments.

Cap open reader browsers at three too, reclaiming the least recently used idle
browser when necessary and explicitly disclosing lost state on reopening.
Assignments initially retain 120 seconds, 24 model turns and configured context
capacity, without separate reader action/page allowances. Context exhaustion
returns available findings and an explicit limit, without silent history
summarization. Reader configuration initially remains gpt-6-luna with low reasoning,
subject to screenshot capability verification. Use asynchronous Python Playwright
with one isolated browser context per reader; the three-session cap applies to
contexts, not necessarily separate browser processes. This trades migration and
browser provisioning work for simpler asynchronous interaction and isolation.
Main Jarvis's existing search implementation stays unless integration requires a
change. A shared browser-process failure can affect multiple reader contexts.

Content-changing actions return fresh readable observations and element references;
screenshots remain on demand. Tabs stay inside each reader context. Retain up to
eight reader conversations with explicit release, never silent eviction. One
assignment runs per reader; busy follow-ups return an error. Action failures are
reported to the reader, not blindly replayed. Acceptance includes real browser
fixtures and the original hosting-and-note scenario, targeting under two minutes
with actual timings and call counts reported. Vault-edit redesign is separate.

These choices supersede the conflicting assigned-page and sequential-execution decisions in
[ADR 0003](0003-isolate-webpage-reading.md) and the enforced fallback contract in
[ADR 0004](0004-enforce-page-reader-coverage.md) for the replacement design.
The operator reviewed [the interview](../../.scratch/parallel-page-readers/design.md)
and consolidated [specification](../../.scratch/parallel-page-readers/spec.md), then
approved the reduced four-ticket breakdown for publication. Ticket 01 is now
implemented and verified on the opt-in single-reader path; see
[verification](../../.scratch/parallel-page-readers/verification.md). Concurrent
scheduling, retained follow-ups, production integration and deployment remain in
tickets 02–04. Acceptance of this design is not evidence of changed live behavior.
