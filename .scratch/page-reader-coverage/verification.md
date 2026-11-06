# Local verification

## Scope

Implemented the final agreed design, including reinstated Q17 and defaults of
24 model attempts, 18 reader actions, 120 seconds and 128,000 context tokens.
This record covers local implementation checks. Subsequent authorized deployment
and GitHub synchronization are recorded in [deployment-verification.md](deployment-verification.md).

## Completed checks

- Focused reader/configuration tests: 125 passed.
  `uv run pytest tests/personal_runtime/test_page_reader.py tests/personal_runtime/test_page_reader_coverage.py tests/personal_runtime/test_config.py -q`
- Real local Chrome acceptance fixture: 1 passed. With
  `JARVIS_TEST_LOCAL_BROWSER=1`, run
  `uv run pytest tests/personal_runtime/test_page_reader_local_browser.py -q`.
  The actual SeleniumPageReader opened a clean anonymous browser, captured the
  owned local page without its hidden billing text, used the Billing tab in a
  fresh capture, and returned a verified quote with display labels. Provider
  responses were scripted; no OpenAI API call was required.
- Ruff checks and formatting passed for all changed Python files.
- `git diff --check` passed (Git reports the repository's existing LF/CRLF
  conversion notice for ADR 0003).

## Full suite

`uv run pytest -q`: **631 passed, 10 skipped** in 225.63 seconds.
Environment/platform-dependent tests were skipped in the default run; the opt-in
local Chrome fixture was run separately and passed. The final focused run above
also covers the last search normalization optimization.

## Limits of this evidence

Scripted model responses prove retrieval/control/accounting behavior, not semantic
accuracy or live-provider acceptance of the chosen context budget. The local Chrome
test exercises the real browser engine and page-control scripts on Windows; the
existing Linux worker process-isolation tests remain platform-gated. A changing
public website was not used as an acceptance oracle.
