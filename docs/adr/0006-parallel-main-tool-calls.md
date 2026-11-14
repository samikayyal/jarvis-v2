---
status: accepted
---

# Run main tool calls concurrently

The operator selected general parallel main-tool execution to reduce repeated
model round trips and overlap independent work. Every prepared tool is eligible,
including terminal commands and writes, under a configurable maximum of five
requested calls per model response by default. Calls needing approval count
toward that maximum. Oversized batches execute nothing and return an error for
each requested call rather than queuing overflow work. The model chooses
independent calls and tools retain necessary synchronization for shared
resources. This deliberately replaces the sequential main-loop
contract while preserving the existing sequential browser-action loop inside
each page reader.

Automatically authorized calls continue running while the operator is shown an
approval for another call. Approval must not wait for those calls to finish.
Approved operations can execute while the next ready approval is presented.
Existing authorization still governs every protected action. Results return to
the model together, ordinary failures remain individual results, and completed
actions must not be replayed after approval. This trades a more involved approval
lifecycle for lower waiting time without a tool allowlist or dependency scheduler.

Human approval waiting retains its existing lack of expiry while running calls
retain their applicable execution timeouts. The runner must own the batch across
approval interactions so cancellation, session reset and shutdown can drain work
without replaying completed effects.

The operator advanced the [consolidated specification](../../.scratch/parallel-tool-calls/spec.md)
to ticketing on 2026-11-10 after the [design interview](../../.scratch/parallel-tool-calls/design.md).
Ticket 01 implements this decision in source. Ticket 02 owns deployment and
visible WhatsApp acceptance; source implementation does not establish activation.
