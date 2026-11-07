# Playwright reader trial path

Tickets 01–02 implement concurrent asynchronous public browser readers as
`PlaywrightReaderTools`. They are available to the main Responses tool loop when
explicitly assembled as an additional prepared tool. The default runner still
constructs the existing page reader. Ticket 02 adds scheduling and retained
follow-ups; production instructions and removal of the old reader belong to
ticket 03. No deployment or production cutover is performed here.

## Provision and run

Use the locked uv environment and Playwright's matching browser revision:

```sh
uv sync --locked
uv run playwright install --with-deps chromium
uv run pytest tests/personal_runtime/test_playwright_browser.py tests/personal_runtime/test_playwright_reader.py tests/personal_runtime/test_concurrent_readers.py
```

On Windows, `uv run playwright install chromium` provisions the browser without
Linux system dependencies. Ubuntu runs Chromium with its sandbox enabled; browser
startup fails rather than silently disabling the sandbox. Playwright uses an
anonymous browser process and a fresh context, with a minimal browser environment,
blocked service workers, downloads and WebSockets, and no operator profile.

The application assembly seam is the existing `additional_tools` parameter:

```python
from jarvis_personal_runtime.config import WebReaderConfig
from jarvis_personal_runtime.playwright_reader import PlaywrightReaderTools
from jarvis_personal_runtime.responses import (
    OpenAIRawResponsesAdapter,
    build_direct_responses_runner,
)

adapter = OpenAIRawResponsesAdapter.from_api_key(existing_key)
readers = PlaywrightReaderTools(WebReaderConfig(), adapter.without_retries())
runner = build_direct_responses_runner(
    existing_key, config, additional_tools=(readers,)
)
# In a controlled request, explicitly instruct main Jarvis to use the trial
# start_browser_reader / browser_reader_status / wait_browser_reader tools.
# Close the externally owned adapter when the enclosing runtime shuts down.
```

The web skill gates these tool definitions through ordinary main-tool discovery.
The controlled integration fixture assembles only the new reader with a second
main tool, delegates a task, does other work while the reader runs, collects the
written result and verifies final browser cleanup. Main tool calls stay sequential;
the reader task itself runs independently.

## Main operations and lifecycle

`start_browser_reader(url, prompt)` validates a public starting URL, receives a
self-contained task prompt and returns an ID immediately. Initially three readers
can run concurrently. Starts beyond capacity fail immediately without a queue.
`browser_reader_status(reader_id)` returns current state and available written
findings. `wait_browser_reader(reader_id, seconds)` waits at most 30 seconds without
cancelling the reader when that wait ends. `cancel_browser_reader(reader_id)` stops
work and closes its browser. `follow_up_browser_reader(reader_id, prompt)` starts
another assignment using the retained conversation and surviving browser state;
a busy reader returns `reader_busy` without queuing the prompt.
`release_browser_reader(reader_id)` cancels work, closes the browser and discards
the conversation/findings. Released and reset IDs return `expired_reader_id`.

`WebReaderConfig` exposes three independent positive integer limits, also accepted
in `[web_reader]`: `max_active_readers = 3`, `max_browser_contexts = 3`, and
`max_readers = 8` (retained conversations). Browser capacity also bounds active
assignments because each requires a context. At conversation capacity, explicitly
release a reader; conversation or findings are never silently evicted. At browser
capacity, a new assignment reclaims the least recently used idle browser, never
one with an active assignment. Result fields `browser_state` and
`browser_state_notice` disclose closed/reclaimed state. A reopened browser loads
the last successfully observed URL with fresh cookies, storage and tabs; the next
reader observation explicitly discloses the lost interactive state.

Each reader currently owns a separate Chromium process and isolated context.
Browser work overlaps; only short context allocation/reclamation is serialized.
There is no shared browser process to propagate failures between readers. A
disconnect or tab crash fails that reader even while it is awaiting a model,
closes its resources, and leaves other readers working. Reader tools cannot start
readers or invoke main tools. The main Responses loop still executes one prepared
tool at a time, so this does not enable concurrent vault or other main mutations.

States are `running`, `completed`, `failed`, `cancelled` and `limit`. Limit reasons
distinguish `time_limit`, `turn_limit` and `context_limit`. Reader configuration is
independent of main model selection: defaults remain `gpt-6-luna`, low reasoning,
120 seconds, 24 model turns, 128,000 context tokens and 8,000 reserved output tokens.
The earlier containing-request deadline wins, including time spent before reader
start. Every follow-up resets the assignment's time and turn budget while the
retained conversation still consumes context. Each browser operation has a separate configurable timeout (15 seconds by
default). No reader page/action/capture allowance or findings schema is applied.
Legacy `WebReaderConfig` fields remain for the old implementation during migration.

Completed browsers remain open through the containing request. Main completion,
timeout, approval suspension or cancellation cancels unfinished work and closes all
reader resources. Completed findings and conversations remain collectable across
ordinary requests in the working session; later follow-ups explicitly reopen a
fresh browser. `/cancel` retains completed findings. `/new`, inactivity reset,
main-context reset and restart discard all readers and close resources. Cleanup
is awaited before a later assignment acquires resources. Interrupted browser tool
calls are recorded as interrupted in retained history and are never replayed.
Cancelled work cannot publish late findings, including a provider returning after
cancellation. There is no background WhatsApp send path.

## Reader observations and authority

The reader receives only its task, initial full readable page content and its own
conversation. HTML conversion preserves headings, tables and link destinations.
Each observation supplies references to actual observed nodes; new observations
invalidate prior references. The browser supports inspect, click, type, select,
scroll, history, reload, tab listing/switching/closing and explicit screenshots.
Links may cross domains. Popups belong to the same isolated reader context.

Oversized initial or updated observations are identified explicitly and retained
whole as snapshots. Optional `search_content` performs literal local search with
character ranges and explicit pagination; `read_content` supplies a selected range.
Snapshot IDs identify complete serialized readable observations, including element
metadata. There is no preliminary relevance filter, mandatory inspection order,
quote validator, coverage gate or automatic final-answer warning. Context is never
silently shortened or summarized. `report_findings` can preserve useful partial
written findings before a later execution limit.

The reader's authority is public research, including website search, filtering and
calculators. Account login, private-account actions, purchases, uploads and resource
creation are excluded in trusted instructions. Sensitive controls are omitted and
rechecked before use. Non-read HTTP requests are blocked except one exact observed
public research form submission: a URL-encoded POST must match the form's action,
expected payload and submitting frame, and be a navigation request. The permit is
consumed once; unrelated scripts and background POSTs remain blocked. Obvious
create/save/send/account/purchase controls are excluded. This conservative guard
supports public forms; arbitrary AJAX write endpoints are outside this trial
path. It is not semantic proof that arbitrary website behavior is read-only.
Web content cannot grant authority. No model-facing arbitrary URL, JavaScript,
terminal, private-tool, web-search or recursive-reader interface exists.

Public URL checks and the existing `PublicNetworkProxy` guard initial loads,
redirects and subsequent traffic. The proxy resolves and validates complete DNS
answers and connects to validated literal addresses, preserving DNS rebinding and
private-network protections. Browser action failures disclose uncertainty and ask
for inspection before another action. Clicks are never automatically replayed.
Crashes fail explicitly with loss of interactive state; no silent browser restart
occurs.

## Screenshot verification

Only `browser_screenshot` captures and sends an image. The next model request
contains a real Responses `input_image` data URL; normal observations remain text.
Viewport images use low detail with a conservative local context reservation.
Provider image/configuration rejection fails explicitly and never switches models
or claims visual inspection. See the official [image input schema](https://developers.openai.com/api/docs/guides/images-vision).

After explicitly authorizing reuse of an existing local API key, run the billed
capability fixture in PowerShell:

```powershell
$env:JARVIS_TEST_READER_MODEL = '1'
uv run pytest tests/personal_runtime/test_playwright_reader.py::test_configured_model_reads_canvas_only_screenshot -q -s
```

The key is read in memory and never printed or written. The fixture draws a code
on a canvas, removes scripts from the reader's readable content, and requires the
configured model to request a screenshot and return the code with its source URL.
On 2026-11-06, `gpt-6-luna` with low reasoning passed in two model turns, returning
`ORCHID 7391` and the fixture URL (29.09 seconds including browser/test setup).
This verifies that model's screenshot path, not factual accuracy on arbitrary pages
or the later hosting-comparison performance target.

## Validation scope

Real Chromium fixtures cover dynamic controls, public form POSTs, fresh observations,
cross-domain history, popups/tabs, explicit screenshots, private redirects and
subrequests, stale references, anonymous state isolation, browser crashes and
cleanup. Model-boundary fixtures cover ordinary findings, private-context isolation,
large initial/updated observations, limits, cancellation, unsupported screenshots
and main-tool coordination. The live provider fixture is opt-in; normal tests make
no provider requests.

Focused `ty check` covers the two new modules. A wider check of the existing
Responses module reports 14 diagnostics; checking the starting commit's module
with the same ty version reports the same 14. This ticket does not claim that the
existing codebase passes a newly introduced repository-wide typecheck.

The focused browser/reader, existing web-boundary and main Responses regression
run on 2026-11-06 completed with 70 passed and 2 skipped. The skipped checks require
explicit browser/model provisioning; the billed reader screenshot fixture was run separately as
recorded above. Lint and formatting checks pass for all modified Python files.

For ticket 01, after resolving all three Spec-review findings and synchronizing deployment
dependencies, the final full suite passed with **651 passed, 11 skipped** in
399.89 seconds. Both review axes have no remaining findings. The verified source
commit is `2953ec18ba77bf6c1649d870c5315685f429b664`; see the
[ticket verification](../.scratch/parallel-page-readers/verification.md).

Ticket 02 adds real-browser tests for three overlapping assignments, independent
out-of-order collection, bounded waiting, configurable capacity, busy follow-ups,
fresh budgets with the main deadline, retained context limits, idle reclamation,
explicit release, concurrent context isolation, surviving tabs, crashes during
model waits, navigation cancellation, interrupted history, late-provider rejection,
runtime `/cancel`/`/new`/inactivity/context resets and restart. Resource verification
observes context-close events, driver process exit and proxy-thread termination.
Its validation and review record is in
[ticket 02 verification](../.scratch/parallel-page-readers/verification-02.md).
