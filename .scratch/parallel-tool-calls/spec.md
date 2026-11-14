# General parallel main-tool calls

Status: accepted design; source implementation in ticket 01, deployment and acceptance in ticket 02. See [verification](verification-01.md).
Date: 2026-11-09 (Asia/Amman).

## Outcome

Allow the main model to request several independent prepared tools in one
response and execute them concurrently. Reduce model round trips and overlap
useful work using the existing runner, dispatcher and approval mechanisms.

Every prepared tool is eligible, including terminal commands, writes and
configured MCP tools. The model chooses independent calls; a call requiring
another call's result belongs in a later model turn. Tools retain synchronization
needed for their own shared resources. Each page reader's browser-action loop
remains sequential, and existing reader capacity limits remain in force.

## Batch limit and results

- Add a configurable maximum number of tool calls per main-model response,
  defaulting to **five**. Count every requested call, including calls needing
  approval; this is not merely a count of currently executing operations.
- Put the configured maximum and independence rule in the model's runtime
  instructions. Enable multiple function calls in Responses requests.
- Validate the batch size before execution. If oversized, execute none and
  return a matching error for each call, explaining the configured maximum so
  the model can correct its next response. Do not silently truncate or queue
  overflow work.
- Preserve the existing total tool-call allowance across the request and
  approval continuations. Count individual calls rather than one unit per batch;
  never exceed the remaining allowance. Calls not attempted receive explicit
  results. Batch rejection must not create an unbounded correction loop.
- Start the authorized calls of a valid batch concurrently. Ordinary tool errors
  are individual results and do not cancel independent siblings.
- Retain results against their call IDs and return the resolved batch together
  in a stable order. Resume the model only when every call has a result. Keep
  existing per-result and total-context limits and the complete provider output.

## Approval while work continues

Start automatically authorized work before displaying a ready approval prompt,
without waiting for that work to finish. The same batch remains alive while the
operator decides, and completed results are retained.

Keep one visible pending action. Show the next ready proposal when the previous
one has been decided. Approval starts that exact protected operation once and
does not delay presentation of the next ready approval until execution finishes.
Rejection skips only the selected action. Existing full-access and saved-
permission rules govern each action; neither authorizes replay of completed or
uncertain effects. Enabling full access also resolves eligible retained proposals
without duplicate execution. Turning it off affects subsequent authorization
according to the existing contract.

Human approval waiting does not expire the pending action or consume the model-
loop request timeout. Running calls keep their applicable execution timeouts;
retaining the batch must not remove those bounds. Timed-out calls report their
outcomes without automatic retry. Model execution after approval retains the
existing request-timeout policy.

Ordinary messages and deterministic approval/status commands retain their existing
behavior. Sibling results accumulate while a prompt is visible; no model
continuation or unsolicited completion message is needed for each sibling.

## Lifetime and cancellation

One retained batch inside the existing Responses runner owns its calls, tasks,
results and unresolved approval proposals. Returning an approval prompt does not
end that batch or close its request-owned readers. Cleanup occurs on actual
request completion, cancellation, timeout, session reset or shutdown.

`/cancel`, `/new` and shutdown must reach and drain outstanding batch work even
when the inbound-message operation that displayed approval has returned. Preserve
known completed results, distinguish never-started calls from attempted calls
whose effects are uncertain, and prevent late replies or automatic replay.
Session reset discards the old session as it does today.

## Implementation boundary

Use one small batch execution path and retained batch state in the existing
runner. Extend its approval continuation and cleanup ownership only as needed.
Add narrow protection for shared tool state where concurrent entry requires it.
Do not add a tool-category allowlist, dependency graph, execution-slot scheduler,
overflow queue, or reader-specific batch API. This change does not include model
tuning, polling redesign, or diagnosis of the separate HTTP timing gap.

## Verification

Verify behavior through existing main-runner, runtime and tool boundaries:

1. Independent slow calls overlap, including terminal/write paths when authorized.
   Tool-owned locking still protects shared state. Results match their call IDs.
2. The configured limit is reflected in instructions and enforced before effects;
   six requested calls with a limit of five all receive errors and none execute.
   Approval calls count, and cumulative request allowances still hold.
3. A prompt becomes visible after authorized siblings start but before they finish.
   Multiple approvals proceed without replaying completed calls or waiting for
   already approved operations to finish. Rejection and full-access transitions
   preserve exact-action semantics.
4. Individual failure or tool timeout preserves sibling results. Human waiting
   does not expire approval. Cancel, reset and shutdown during visible approval
   drain tasks/readers and prevent late completion or replay.
5. Single-call behavior, existing approval choices, tool visibility, output limits,
   ordinary-message handling and retained-reader behavior remain coherent.

After an authorized deployment, repeat the original hosting-and-note request with
a fresh session and comparable starting note. Record call counts, actual overlap,
runtime/delivery evidence, successful note save/sync and answer quality. The
two-minute goal remains a measured benchmark target; it does not expand scope.
Deployment must follow the repository's committed/pushed, exact-GitHub-SHA
verification rule and synchronize the resulting deployment documentation.

## References

- [01: Execute parallel tools while approvals remain interactive](issues/01-execute-parallel-tools-through-approvals.md) — no blockers.
- [02: Deploy and measure parallel tool calls in WhatsApp](issues/02-deploy-and-measure-parallel-tool-calls.md) — blocked by 01.
- [Interview decisions](design.md)
- [Accepted architectural decision](../../docs/adr/0006-parallel-main-tool-calls.md)
- [Latency analysis](../parallel-page-readers/latency-recommendations-2026-11-08.md)
