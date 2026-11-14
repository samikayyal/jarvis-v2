# 01: Execute parallel tools while approvals remain interactive

**What to build:** Jarvis can execute independent prepared tool calls from one
main-model response concurrently, including terminal commands and writes. It
starts authorized work and presents approvals while that work continues, retains
every result, and resumes the model after the batch is resolved. Deliver the
complete behavior with configuration, documentation and focused regression
coverage, using the accepted general parallel main-tool design and ADR 0006.

**Blocked by:** None (can start immediately).

**Status:** complete

- [x] The main model can request multiple calls in one response. Every prepared
  tool is eligible, with the model choosing independent calls and tools retaining
  necessary protection of shared resources. Meaningful regressions prove actual
  overlap, including authorized terminal and write calls. Each reader's own
  browser-action loop and existing reader capacity limits remain unchanged.
- [x] A configurable maximum defaults to five requested calls per response,
  counting approval-requiring calls too. Model instructions state the configured
  limit and require dependent calls to wait for a later model turn.
- [x] An oversized batch is rejected before any tool effect, with a matching
  error for every call and guidance to submit a smaller batch. There is no
  partial execution, silent truncation or overflow queue. Total request
  allowances continue counting individual calls across approvals, and correction
  attempts remain bounded.
- [x] Automatically authorized calls start before a ready approval is shown,
  and keep running while the operator decides. The prompt is visibly available
  through the runtime's existing delivery path before slow siblings finish;
  entering approval does not close the batch or its readers.
- [x] Only one approval is displayed at a time. Approval starts the selected
  action once while the next ready approval can be shown immediately; rejection
  skips only that action. Existing approval choices, saved permissions and full-
  access transitions apply without duplicate execution or replay.
- [x] The model resumes only after every batch call is resolved. Results retain
  their call IDs and stable ordering, ordinary failures preserve successful
  siblings, and existing output/context limits and complete provider output are
  preserved. Uncertain side effects are never automatically retried.
- [x] Human approval waiting does not expire the pending action or consume the
  model-loop request timeout. Active calls retain applicable execution timeouts
  and their results remain available while approval is pending.
- [x] Cancel, session reset and shutdown stop and drain outstanding work even
  while approval is displayed. Cleanup preserves known outcomes, distinguishes
  never-started calls from uncertain attempts, and prevents late replies or
  surviving request-owned tasks/readers.
- [x] Single-call requests, ordinary-message handling, deterministic commands,
  tool visibility and retained-reader behavior pass relevant regressions. The
  required runtime checks pass, with any pre-existing failures documented
  separately from introduced failures.
- [x] Domain, runtime-skill and operational documentation describe the new
  source behavior accurately and distinguish it from the still-active release.
  Keep this one focused implementation: reuse the current runner/dispatcher and
  retain one active batch; do not add a tool allowlist, dependency scheduler,
  reader-specific batch API or unrelated latency changes.

## Comments

2026-11-14: Implemented and reviewed on `main`. See
[verification](../verification-01.md) for overlap, approval delivery, limits,
cancellation/draining, real-reader lifecycle tests, full-suite results and
focused reruns. Standards and Spec reviews reported no actionable findings.
Existing typecheck diagnostics and a transient reader-test readiness timeout
are recorded separately. Deployment and live WhatsApp acceptance remain ticket 02.
