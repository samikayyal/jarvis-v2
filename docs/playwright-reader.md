# Concurrent Playwright readers

The normal Responses runner composes `WebTools` for main Google search and
`PlaywrightReaderTools` for public-page investigations when web search is enabled.
Loading the packaged web skill exposes all six reader lifecycle tools. The old
reader and its quote, fallback, chunk and coverage policies are removed in ticket 03.
This describes repository behavior. Ubuntu deployment and live acceptance remain
in ticket 04; see [predeployment verification](../.scratch/parallel-page-readers/verification-03.md).

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

Normal service assembly uses the configured independent reader model through the
same provider adapter with automatic retries disabled. No additional-tools setup
is needed. Main tool calls stay sequential while reader tasks run independently.
The default-assembly integration fixture starts two readers, reads vault context
while they work, synthesizes a concise answer, releases one conversation, and
checks that remaining browsers close while completed findings survive.

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
supports public forms; arbitrary AJAX write endpoints are outside this browser
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
cross-domain history, tabs, screenshots, overflow, context isolation, limits,
crashes, cancellation, retained follow-ups and actual resource teardown. Scripted
provider boundaries make ordinary tests reproducible without billed API calls.
The opt-in screenshot test above records historical capability verification; this
ticket does not repeat it or claim live WhatsApp acceptance.

Focused typechecking covers Playwright readers/browser, HTML conversion and main
search. Existing broader runtime/Responses type diagnostics are recorded separately.
The source, full-suite result, review and reproducible commands for this candidate
are recorded in [ticket 03 verification](../.scratch/parallel-page-readers/verification-03.md).
