# Conversational page reader

Load the web skill, search for sources, then call `read_web_page(url, query)`.
Supply the focused question and necessary context. The reader sees only that
page, that query and its own conversation. It cannot search, navigate to another
page, access private tools, or delegate to another reader. Main Jarvis follows
returned links and combines findings.

Results contain `reader_id`, `outcome`, requested/final URLs, `answer`, verified
source quotes with chunk locations and display labels, `gaps`, `links`, and
`coverage`. Coverage separates inspected chunks from captured chunks and reports
incomplete capture. Finding no answer in selected chunks does not prove absence.

`ask_page_reader(reader_id, query, refresh=false)` reuses captured evidence and
can inspect unread chunks without fetching. Explicit refresh requires a
self-contained query: successful capture replaces history while keeping the ID;
failed refresh retains old evidence but returns no fresh claims. Calling
`read_web_page` again always creates a separate reader. `/new`, inactivity,
main context reset and restart discard the registry. Cancellation interrupts
active work and closes its browser; completed evidence remains until reset.

Small documents are supplied whole. Larger documents expose a heading index
and a reader-only `read_page_chunk` operation. Conversion preserves headings,
paragraphs, tables and link destinations. Oversized sections split at paragraphs
or rows, repeating headings and table headers. Selected chunk bodies stay in the
reader conversation; context exhaustion never silently summarizes or evicts them.

The reader can use `display_page` to render expected dynamic text, expand a
listed section, choose a listed region option or switch a listed tab. Controls
inside forms and links are excluded. This is additional network work, disclosed
as `page_work`; evidence retains the selected display labels. Public URL, DNS,
redirect, challenge, sandbox, download and anonymous-profile protections remain
in the existing browser worker. Login and submitting user data are excluded.

Configure `[web_reader]` with `model = "gpt-6-luna"` and
`reasoning_effort = "low"`. Reader selection is independent of `/model`.
Missing/unsupported configuration fails explicitly, without model fallback.
Defaults in `WebReaderConfig` are 12,000 tokens per document/chunk, 64,000 context
tokens, 8,000 reserved generation tokens, 2,000 returned findings tokens,
120 seconds per invocation, eight model attempts, eight reader actions, eight
retained readers, and 1,000,000 converted characters per reader. There is one
transient retry per invocation; display actions are not automatically replayed
after an uncertain failure. Provider automatic retries are disabled for readers.

The earlier outer request/browser deadline wins. Captures share existing
request-owned page allowances; local chunk reads do not. Captured content and
compact findings have separate budgets. Limit outcomes preserve prior evidence
and require an explicit next step. Findings are shortened as structured JSON,
with source identity retained and shortening disclosed.

Existing runtime traces record model, elapsed seconds, attempts, actions,
page work, coverage, outcome and available provider token usage. No monetary
cost is inferred without a verified applicable price.

The accepted [spec](../.scratch/page-reader/spec.md) and
[ADR](adr/0003-isolate-webpage-reading.md) record the design and acceptance scope.
