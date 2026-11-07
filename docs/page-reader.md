# Concurrent page readers

Normal web-assisted requests use concurrent Playwright readers. Load the web
skill, discover sources with main `search_web`, then delegate focused work using
`start_browser_reader(url, prompt)`. Start returns immediately. Main Jarvis may
start independent investigations, do its own work, collect each result with
`browser_reader_status`, or use bounded `wait_browser_reader` when it has no other
work. Follow up with `follow_up_browser_reader`, stop work with
`cancel_browser_reader`, and explicitly discard conversations with
`release_browser_reader`.

Readers receive their focused prompt, the full readable starting page and their
own conversation. They may follow public links across domains and use public
search/filter/calculator controls. Initial pages and subsequent observations
preserve headings, tables and links. Screenshots require an explicit request.
Oversized observations are retained whole and marked overflow, with optional
local search/selected reading. Written findings include source URLs, optional
excerpts and specific uncertainty. Completion is an execution state, not proof
that findings are correct or sufficient; main Jarvis makes that judgment.

Defaults: three active assignments, three browser contexts, eight retained
conversations; 120 seconds and 24 model turns per assignment; 128,000 context
tokens with 8,000 reserved output tokens. The earlier main deadline and browser
operation timeouts still apply. Follow-ups get fresh assignment time/turn budgets
and reuse history without silent summarization or eviction. Busy assignments and
capacity failures do not queue work. Idle browser contexts may be reclaimed;
reopening discloses lost interactive state while preserving findings/history.

Completion, suspension, timeout and cancellation stop active readers and close
browsers. Completed findings survive the working session. Release expires one
ID; `/new`, inactivity reset, main-context reset and restart discard all IDs.
The main loop and vault writes remain sequential. Public-network, anonymous-state,
challenge and action-authority protections remain in force.

## Configuration migration

Keep `[web_reader] model = "gpt-6-luna"` and `reasoning_effort = "low"` (on separate
TOML lines). Model selection is independent of `/model`; missing configuration
fails explicitly and never selects another model. Remove the retired
`chunk_tokens`, `max_actions`, `findings_tokens` and `capture_chars` keys from
existing `[web_reader]` tables before activation. Configuration checking names
these keys and fails until they are removed. `web_search.*.max_pages` remains
accepted for compatibility, but does not impose reader page counts. Existing
search query/snippet limits still apply to main Google search.

The assigned-page chunk reader, `read_web_page`, `ask_page_reader`, old batch-read
interface, quote-occurrence validation, mandatory fallback searches/sweeps,
coverage accounting and required findings schema are removed. Runtime answers
are no longer modified with automatic web/source warnings. The agents must
report consequential uncertainty naturally and respect requested answer length.

See [browser operations and provisioning](playwright-reader.md),
[main search](web-search-tool.md), and [ADR 0005](adr/0005-simplify-and-parallelize-page-readers.md).
This describes repository implementation. Ubuntu activation and live performance
acceptance remain ticket 04; [ticket 03 verification](../.scratch/parallel-page-readers/verification-03.md)
is predeployment evidence only.
