# Ticket 03 predeployment verification

Date: 2026-11-07 (Asia/Amman).

Scope: normal runtime integration of concurrent readers and removal of the
superseded reader. This is local predeployment evidence. No GitHub push, Ubuntu
activation, live WhatsApp acceptance or hosting-performance claim is part of
ticket 03. Ticket 04 owns those steps and the exact-SHA GitHub activation gate.

Review baseline: `80af6449de38c29a5721e2a1063bd706336798c5`, the clean current
branch at task start. Initial implementation: `85c51c6672c71204639b4345b2bd5a1214e74f3c`.
Reviewed source candidate: `aaef0149fa531ac6008b16dc5a15eadb2541a5e5`.
The final full-suite result is recorded below. Completion documentation
may use a later commit without changing this verified source.

## Implementation

- Default service assembly uses main Selenium Google search alongside concurrent
  Playwright readers. Web-skill discovery exposes start/status/wait/follow-up/
  cancel/release. A scripted-provider/real-browser scenario delegates twice,
  reads actual vault context while readers run, synthesizes a concise answer,
  releases one reader, and checks retained findings and browser closure later.
- Progress defaults to 20 seconds and reflects the owning active request or
  observed reader-state counts. Repeated state is suppressed. Progress sends
  drain before final/cancel delivery; busy rejection and status/cancel remain.
  Cancellation/session resets invalidate any reply still awaiting transport,
  checked under the send lock before each chunk, including after model completion.
- Deleted the old reader, chunk splitter, structured Selenium capture/display
  interface, quote validation, mandatory fallback/sweeps, coverage accounting,
  required findings schema, reader action/page allowances and automatic answer
  warning hooks. Optional overflow search and selected reading remain.
- Retired reader configuration fails validation with named keys to remove:
  chunk_tokens, max_actions, findings_tokens, capture_chars. Missing reader model
  configuration fails explicitly without fallback. Main search retains its
  query/snippet limits, anonymous worker, proxy, challenge and cleanup behavior.
  Existing max_pages search keys are accepted for deployed TOML compatibility,
  with no effect on reader assignments.
- Vault proposal/approval/commit/sync implementation is unchanged. Main tools
  remain sequential. Domain terms, ADR supersession, packaged instructions and
  provisioning/migration operations describe repository behavior separately
  from deployment.

## Reproduce

Use Python 3.13 with the locked uv environment and provisioned Playwright
Chromium (`uv sync --locked`, then `uv run playwright install chromium` on
Windows, or `uv run playwright install --with-deps chromium` on Ubuntu).

```console
git rev-parse HEAD
uv run pytest tests/personal_runtime/test_reader_integration.py tests/personal_runtime/test_openwa_message_flow.py tests/personal_runtime/test_web_tools.py tests/personal_runtime/test_web_responses.py tests/personal_runtime/test_config.py
uv run pytest tests/personal_runtime/test_playwright_browser.py tests/personal_runtime/test_playwright_reader.py tests/personal_runtime/test_concurrent_readers.py
uv run ty check src/jarvis_personal_runtime/playwright_reader.py src/jarvis_personal_runtime/playwright_browser.py src/jarvis_personal_runtime/web_tools.py src/jarvis_personal_runtime/page_document.py
uv run ruff check src/jarvis_personal_runtime tests/personal_runtime deployment/personal-runtime/verify-web.py
uv run ruff format --check src/jarvis_personal_runtime tests/personal_runtime deployment/personal-runtime/verify-web.py
uv run pytest tests/personal_runtime
uv build --wheel --out-dir .scratch/parallel-page-readers/artifacts
git diff --check
```

## Typecheck boundary

The focused modules pass. The wider Responses/runtime/OpenWA/config check reports
24 existing diagnostics, versus 25 in the baseline under the same project and
Python environment. Comparing normalized messages finds no added message; the
removed diagnostic is the old PageReaderTools/PreparedTools protocol mismatch.
This is not a repository-wide typecheck pass. Saved outputs:
[baseline](verification/baseline-typecheck-03.log) and
[candidate](verification/candidate-typecheck-03.log).

## Timing and model-call instrumentation

Main `responses_request`/`responses_output` events include run_id and model_call;
outputs include call elapsed_seconds. Reader request/output events include
reader_id, assignment_id and model_call, and output elapsed_seconds.
`playwright_reader_finished` records assignment wall time, state, reason, model
and model_turns. Assignment IDs distinguish follow-ups on the same reader. These
are execution measurements and do not claim semantic verification.

For the ticket 04 comparison, isolate the admitted message's trace interval from
request_started through its final openwa_send_result. Count responses_request
events for main calls and playwright_reader_request events for reader calls,
grouping readers by assignment_id. These count logical model calls. Main SDK
retries can produce additional HTTP attempts; inspect http_attempt_request and
responses_raw_exchange.retries_taken separately. Reader SDK retries are disabled.
Sum provider usage only when present. Reader
wall time includes browser and model time; do not add nested model time again.
Measure total admission-to-final-delivery wall time from timestamps, and retain
tool timings for vault approval/edit/sync separately. The historical 458-second,
22-main-call/27-reader-call baseline and under-two-minute target require a live
repeat; fixture timings do not establish that target.

The updated installed-wheel verify-web.py smoke probe uses scripted main and
reader providers, live public browsers only with --live, no keys and no messages.
It exercises current start/wait/retained follow-up/fresh recheck and browser closure.
The opt-in model screenshot fixture remains separate; its historical authorized
capability result is in docs/playwright-reader.md. No billed check was run here.

## Final validation and review

Focused configuration/progress/search/skill checks: 119 passed. Real browser,
reader, concurrent lifecycle and default-assembly integration checks: 34 passed,
1 skipped (345.78 seconds). Smoke-probe contracts, search regressions and added
assembly/instrumentation checks: 23 passed. Cancellation-fix OpenWA/runtime/full
access regressions: 43 passed. Focused typechecks, lint, formatting and diff checks
pass. The default smoke probe ran without --live or credentials; its report
records Windows source-path provisioning observations, not installed Ubuntu proof.

### Standards

Independent review of the complete implementation and follow-up: 0 hard violations
and 0 actionable smells. Domain vocabulary, issue-tracker conventions, uv use,
ADR supersession and predeployment/deployment boundaries follow repository rules.
Progress forwarding uses the existing prepared-tool composition boundary. Runtime
owns reply invalidation and OpenWA checks it under the send lock; assignment IDs
are available before immediate start/follow-up returns.

### Spec

Independent review reproduced one P1: when a progress send was blocked, model
completion cleared the active request; /cancel or /new could reply before the
old final answer. Red regressions reproduced both commands. Delivery-generation
invalidation now discards the final and checks each chunk before starting a send.
The reviewer independently reran 5 relevant regressions (12 deselected), confirmed
the fix and reported 0 remaining actionable Spec findings. No additional scope
creep or omissions were found. This review is not live acceptance.

Summary: Standards 0 findings; Spec 1 P1 found and resolved, 0 remaining.
The first complete suite on initial implementation 85c51c6 passed: **549 passed,
9 skipped in 547.34 seconds**. It preceded the reviewed cancellation fix and is
retained as [initial output](verification/full-suite-initial-03.log), not the
final candidate result. Final manager/integration checks after the fix: **15
passed in 159.06 seconds**.

The wheel was rebuilt from a fresh tracked-source export of candidate aaef014,
then all 31 packaged Python/skill files were compared byte-for-byte to that
commit. Retired page_reader.py and stale build-only skills are absent.
[Wheel record](verification/wheel-03.json) records the candidate and SHA-256
`f66f9a93392e8ddb5d09b353c9cca63733b6eb0e8bd36aa14e66ba23d74792b2`.
The artifact is local under artifacts/ and is not an activated release. Build
from a clean checkout/export to avoid stale setuptools build-directory files.
Final full suite on reviewed source **aaef0149fa531ac6008b16dc5a15eadb2541a5e5**:
**551 passed, 9 skipped in 469.28 seconds**. See [saved output](verification/full-suite-03.log).
The skips are the explicitly billed model screenshot fixture (1), native Ubuntu
process-group test (1), a provisioned Chromium/ChromeDriver test (1), and POSIX
installed-browser fixtures (6). No source or test changes followed this run.

Final repository lint, formatting, focused typechecks and diff checks pass.
Post-suite inventory found **zero Playwright browser/driver worker processes**;
see [resource record](verification/resources-03.json). Real-browser tests also
assert context-close events, driver exit and proxy-thread termination.

A direct wheel import from an isolated working directory, with Python isolated
mode and existing environment dependencies, passes the no-live smoke probe:
[wheel import report](verification/wheel-import-probe-03.json). A separate fresh
uv dependency installation was stopped while downloading the 35.1 MiB Playwright
wheel; offline provisioning confirmed that archive was absent from cache. No
isolated installation or Ubuntu installed-artifact pass is claimed. Ticket 04
must provision the matching browser/system dependencies and run the staged
installed-candidate checks under the service account before activation.

The candidate is ready for ticket 04 staging and environment-specific checks.
No candidate push, Ubuntu activation, live WhatsApp/Obsidian acceptance or
under-two-minute performance attainment is claimed. The local completion commit
records verification only; it does not alter the source candidate above.
