# Ticket 01 implementation verification

Implementation: 2026-11-10. Final verification and completion: 2026-11-14 (Asia/Amman).
Scope: source implementation and local validation; no push, Ubuntu activation or live WhatsApp acceptance.
Starting commit: `be4365d7d194cbf584dcfac52cfcc700fef683fd` on `main`.

## Implementation

The existing Responses runner owns one retained batch. It starts independent
prepared calls concurrently, returns one ready approval through the existing
runtime/OpenWA delivery path, and preserves sibling work until all results can
return in call order. The configured maximum defaults to five, includes approval
calls, and rejects the whole oversized batch before effects. Individual requested
calls consume the cumulative request allowance, including rejected batches.

Approval decisions start the selected action once and can expose the next proposal
before the approved operation finishes. Full access resolves retained proposals.
Human waiting is outside model-loop timeouts; active calls retain tool timeouts and
a request-timeout execution bound. Cancellation, session reset and shutdown drain
retained work, including a second cancellation during timeout cleanup. Completed
results are preserved; never-started actions and uncertain attempts are distinct.

Search serializes the shared browser, block state and allowance accounting. Vault
clone locking, reader capacities and sequential reader browser actions stay intact.

## Behavioral evidence

Tests use the existing runner, runtime, prepared-tool and message-flow boundaries:

- Authorized terminal writes and a real prepared Google calendar write overlap
  using controlled executor/HTTP boundaries.
- Default/custom caps reject all excess calls without effects, count approvals,
  bound correction attempts and expose the configured rule in model instructions.
- OpenWA message flow delivers approval before a slow authorized sibling finishes.
- Multiple approvals, rejection, full access, ordinary-message ignoring, cumulative
  allowance, failures and exact call-ID ordering are covered.
- A timed-out sibling is retained while human approval remains valid. Cancellation
  retains known success, labels uncertain work and never-started proposals, and
  does not replay either.
- Real Playwright readers stay running with open browsers during approval and
  drain on cancellation, reset or shutdown. Completed findings survive rejection
  and normal completion; reader model requests still disable parallel actions.
- A red regression reproduced repeated cancellation interrupting executor cleanup;
  shielded request draining fixes it and waits before releasing ownership.

Related regression run: **196 passed** (config, Responses, runtime, full access,
web tools, web lifecycle and skill visibility). The parallel-tool file passed
18 tests before the final cleanup/cap additions; final suite results are below.
An initial real-reader fixture hit its 20-second model-arrival wait; all four
reader cases passed on rerun. No live external service was mutated by these tests.

## Static checks

Ruff lint, Ruff format, compileall and Git whitespace checks passed.
`uv run ty check src/jarvis_personal_runtime` remains non-green due to existing
repository diagnostics. An isolated archive of the starting commit, checked with
the same environment, reports 48 diagnostics; candidate source reports 46, with
zero introduced diagnostics (two old continuation-type diagnostics removed).
See [baseline](typecheck-baseline.txt) and [candidate](typecheck-current.txt).

## Review

The implement skill's code-review stage used independent Standards and Spec
reviews against the starting commit and candidate diff. Standards: zero actionable
findings. Spec: zero confirmed missing, partial, incorrect or extra-scope behavior.
Both reviews also examined the shielded-cleanup fix and reported zero findings.

## Final validation

- Full suite, `uv run pytest -q --tb=short`: **575 passed, 9 skipped, 1 failed**
  in 798.62 seconds. The failure was the old main-reader integration test, whose
  scripted provider asserted `parallel_tool_calls is False`. That expectation
  now asserts True; its single-call and reader-cancellation checks are preserved.
- Follow-up run of that corrected test plus the parallel-tool file: **21 passed,
  1 failed**. All parallel-tool tests passed; the corrected integration test still
  hit its 30-second readiness wait. A temporary in-memory trace then reached all
  expected tool stages and passed, without changing runtime source or its limits.
- Final normal rerun of
  `test_main_runtime_single_call_turns_and_cancel_stops_readers`: **1 passed in
  9.91 seconds**. The earlier readiness timeout is retained here as a transient
  test observation; its underlying timing cause was not established. No full-suite
  green rerun is claimed. All test cases have passing results across the broad run
  and the focused reruns.
- Final Ruff lint, format (68 files), compileall and staged/working-tree whitespace
  checks passed. Typecheck comparison remains 48 baseline / 46 candidate diagnostics
  with none introduced.

Ticket 01 is complete. The implementation is committed locally on `main`;
commit identity can be obtained from the commit containing this record.
Deployment and visible WhatsApp benchmark remain ticket 02 work.
