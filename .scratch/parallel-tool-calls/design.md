# General parallel tool calls: design interview

Status: accepted design; approved two-ticket breakdown published.
Date: 2026-11-09 (Asia/Amman).

## Agreed direction

The operator wants to align on general parallel tool calls as the first, simple
latency improvement. Avoid overengineering. Use the existing prepared tools and
model-and-tool loop where practical.

The preceding latency investigation is recorded in
[the analysis](../parallel-page-readers/latency-recommendations-2026-11-08.md).
Its recommendations are context, not automatically accepted design decisions.

The [consolidated specification](spec.md) describes the final selected behavior.
The rounds below preserve how that behavior was agreed; later corrections
supersede earlier proposals.

## Round 1: accepted

The operator answered "yes to all" to Q1 and Q2.

- **Q1 — Scope:** Support multiple prepared tool calls in one main-model response
  and actual concurrent execution of independent calls. Each page reader's
  browser-action loop remains sequential.
- **Q2 — Success:** Keep this a focused main-loop improvement. Verify actual
  overlap, approval behavior and cancellation, then measure the original
  hosting-and-note task's model-call count and completion time. The two-minute
  goal remains a benchmark target; it does not expand this implementation into
  unrelated latency work.

## Existing contract to revisit

At the design checkpoint, root `CONTEXT.md` defined the model-and-tool loop as sequential, with
one prepared tool call at a time and parallel tool calling disabled. The runner
also rejected multiple function calls in one response. The proposed work changes
that contract; documentation must describe the agreed behavior explicitly.

The runtime currently permits one pending action. General parallel execution
must have an agreed interaction with this existing approval contract.

## Round 2: accepted with a correction

The operator answered "yes to all except 3 everything is parallel".

- **Q3 — Correction:** Every prepared tool is eligible for parallel execution,
  including terminal commands and writes. Do not introduce a tool allowlist or
  serialize a category merely because it can write or require approval.
  The proposed separate three-call concurrency cap was part of Q3; its status
  needs clarification rather than assuming it remains accepted.
- **Q4 — Approval intent:** Keep one visible pending action, execute an approved
  action once, skip a rejected action, and preserve completed results. `/cancel`
  stops the remaining batch. The previously proposed ordered stopping point
  conflicts with launching every call concurrently, so the ordering detail must
  be revisited explicitly. Do not silently retain a serial approval barrier.
- **Q5 — Collection:** Return the batch's results to the model together rather
  than streaming partial results or resuming the model on each completion.
- **Q6 — Failure and allowance:** Ordinary tool failures are individual results;
  independent siblings continue. The existing allowance counts individual tool
  calls. Excess calls are not attempted. Cancellation or request timeout stops
  and awaits active calls before cleanup.

## Round 3: accepted with corrections

- **Q7 — Prompt while work runs:** Start automatically authorized calls and
  present required approval after they start, without waiting for them to finish.
  Automatically authorized work continues while the operator decides. This
  supersedes both the ordered stopping point from Q4 and the proposal to finish
  automatic work before showing approval. No action needing approval executes
  its protected effect before the existing authorization check succeeds.
- **Q8 — Configurable concurrency:** Provide a configurable maximum number of
  parallel tool calls. The proposed absence of a separate cap was rejected.
  Default value, slot accounting and behavior when the batch exceeds the cap
  remain to be settled.
- **Q9 — Independence:** The model chooses independent calls. Calls needing
  another result belong in a later model turn. All prepared tools are eligible
  for parallel dispatch, while their necessary internal synchronization remains.
  Do not add a dependency graph or tool-category allowlist.

## Round 4: accepted with corrections

- **Q10 — Five requested calls:** The configurable limit defaults to five and
  counts every tool call requested in a single model response, including calls
  needing approval. It is a maximum batch size, not a count of currently occupied
  execution slots. Do not exclude pending approvals from this count.
- **Q11 — Reject excess:** Clearly instruct the model about the configured
  maximum. If it requests more calls in one response, return an error instead
  of queuing overflow calls. Enforce this locally by counting calls before any
  execution; a prompt instruction alone is insufficient. The proposed whole-
  batch rejection/error details still need final confirmation.
- **Q12 — Multiple approvals:** Show one approval at a time. Once approved,
  let that action execute while presenting the next ready approval; do not wait
  for the approved action to finish first. Retain completed results and resume
  the model only after the entire batch is resolved.

These corrections remove the proposed execution-slot scheduler and overflow
queue. A valid batch contains at most the configured number of calls, so
dispatching its authorized operations together respects that bound directly.

## Round 5: accepted

The operator answered "yes to both" to Q13 and Q14.

- **Q13 — Whole-batch rejection:** Reject an oversized batch before any tool
  executes. Return a matching error result for every requested call, state the
  configured maximum, and let the model correct its next response. Do not run
  the first five calls, silently truncate, or queue the excess.
- **Q14 — Approval waiting and cleanup:** Human approval waiting does not expire
  the pending action or consume the model-loop request timeout. Running calls
  retain their applicable execution timeouts and keep their results. A timed-
  out call reports failure without automatic replay. `/cancel`, `/new` and
  shutdown stop and drain outstanding work even while an approval is visible.

## Final checkpoint

On 2026-11-10 the operator advanced the consolidated design to ticketing,
requested as few tickets as possible, and approved the two-ticket breakdown.
Both tickets are published locally as `ready-for-agent`:

1. [Execute parallel tools while approvals remain interactive](issues/01-execute-parallel-tools-through-approvals.md) — no blockers.
2. [Deploy and measure parallel tool calls in WhatsApp](issues/02-deploy-and-measure-parallel-tool-calls.md) — blocked by ticket 01.

At this ticketing checkpoint, implementation and deployment had not started.
The current ticket status and verification record below supersede that checkpoint.

## Facts established during the interview

- `PreparedToolCollection` already dispatches all existing tool names and wraps
  approval continuations; a new general scheduling framework is not required.
- The main runner's approval continuation retains one call and one tool
  continuation. Supporting a suspended batch requires tracking its remaining
  calls and already completed results.
- Web searches share a browser and request allowances. The browser RPC lock
  serializes individual worker operations, but whole-search allowance/evidence
  accounting also needs serialization if calls can enter concurrently.
- The Git-backed vault already serializes reads and writes with an async lock.
  A tool being read-only is not by itself proof that its implementation can
  execute concurrently with another call.
- Returning an approval currently runs `_close_request()` and clears the
  runtime's active inbound-message task handle. Reader work is cancelled by
  that cleanup. The accepted background-work behavior therefore requires
  retaining ownership of the batch across approval interactions, including a
  cancellation/drain path independent of the inbound-message task.
- Existing approval waiting has no request timeout or inactivity expiry; normal
  model-loop execution receives a fresh timeout after approval resumption.
- `/cancel`, `/new` and shutdown must reach the retained batch. Existing full-
  access and saved-permission rules must still authorize each exact action;
  enabling full access must not execute a retained proposal twice.
- Reader assignments already have their own limits. A reader-start invocation
  counts as one main tool call; its delegated assignment is not an additional
  main call or a new ordinary request.

## Documentation approach

Record answers here as they settle. Use the repo's root `CONTEXT.md` for settled
domain terminology rather than creating a competing glossary. Create an ADR only
if the final choice contains a meaningful, costly-to-reverse architectural
trade-off. Do not describe proposed behavior as already implemented.

## Source implementation checkpoint: 2026-11-10

Ticket 01 implements the accepted batch and approval lifecycle. See
[verification](verification-01.md) for tests, review and pre-existing typecheck
diagnostics. Ticket 02 owns deployment and live acceptance; neither is established
by the source change.
