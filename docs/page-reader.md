# Conversational page reader

Load the web skill, search for sources, then call `read_web_page(url, query)`.
Supply the focused question and necessary context. The reader sees only that
page, that query and its own conversation. It cannot search the web, navigate to another
page, access private tools, or delegate to another reader. Main Jarvis follows
returned links and combines findings.

Results contain `reader_id`, `outcome`, requested/final URLs, `answer`, verified
source quotes with chunk locations and display labels, `gaps`, `links`, and
`coverage`. Coverage separates inspected chunks from captured chunks and reports
incomplete capture. `answered` describes the model's question-level assessment
after quote validation; `stop_reason` distinguishes success, captured-content
exhaustion and limits. Finding no answer in selected chunks does not prove absence.

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

The reader-only `search_page(terms)` searches all retained chunks locally using
1-20 literal terms/phrases of at most 200 characters. Matching ignores case and
normalizes whitespace. It returns only unique matching chunk IDs, in capture order,
so the reader can consult its heading index before reading. Results contain no
excerpts or ranking; no embedding service is involved. An empty result does not
establish semantic absence. A search result too large for context fails explicitly
rather than silently omitting matches. Search does not mark chunks inspected.

An unresolved result triggers a code-controlled fallback: inspect pending matches,
require up to two related-term search rounds if captured chunks remain unread,
review relevant display controls/rendering, then supply remaining chunks one at a
time in capture order. Searches already performed count toward the two rounds.
The model chooses search terms and relevant display actions; application code
controls progression. Repeated refusal to take a required step ends at the shared
limits rather than becoming a successful absence claim. Previously supplied chunks
are reused without duplicating their complete bodies in context.

Supported completion requires an explicit `answered=true`, at least one quote that
occurs in inspected content, and no reported gaps. Invalid quotes are rejected;
an answer without validated evidence remains unresolved. Occurrence validation
cannot prove that a quote entails every claim or that the model understood it.
Follow-up invocations assess their own question and never inherit a prior answer's
completion status. There is no per-question-part checklist.

The reader can use `display_page` to render expected dynamic text, expand a
listed section, choose a listed region option or switch a listed tab. Controls
inside forms and links are excluded. This is additional network work, disclosed
as `page_work`; evidence retains the selected display labels. Public URL, DNS,
redirect, challenge, sandbox, download and anonymous-profile protections remain
in the existing browser worker. Login and submitting user data are excluded.

Coverage includes separate capture IDs, display labels, inspected counts, capture
truncation, rendering wait status and failed display operations. `all_captured`
means all retained chunk bodies were supplied, not that every possible website
state was explored or that the information is absent. Search hits alone do not
count as inspected bodies.

Configure `[web_reader]` with `model = "gpt-6-luna"` and
`reasoning_effort = "low"`. Reader selection is independent of `/model`.
Missing/unsupported configuration fails explicitly, without model fallback.
Defaults in `WebReaderConfig` are 12,000 tokens per document/chunk, 128,000 context
tokens, 8,000 reserved generation tokens, 2,000 returned findings tokens,
120 seconds per invocation, 24 model attempts, 18 reader actions, eight
retained readers, and 1,000,000 converted characters per reader. There is one
transient retry per invocation; display actions are not automatically replayed
after an uncertain failure. Provider automatic retries are disabled for readers.

The earlier outer request/browser deadline wins. Captures share existing
request-owned page allowances; local chunk reads and text searches do not. Searches,
chunk reads (including automatic fallback reads) and display operations share the
reader action limit; fallback never resets any allowance. Captured content and
compact findings have separate budgets. Limit outcomes preserve prior evidence
and require an explicit next step. Findings are shortened as structured JSON,
with source identity retained and shortening disclosed. Source identity, completion
status and coverage/stop facts take precedence over optional prose. If essentials
cannot fit, return `output_limit`, not apparent success with missing coverage.

Unresolved source checks produce code-owned final-answer notices that cannot be
lost during tool-output shortening. They apply to the named source, even if another
source answers the question. A successful continuation of the same question clears
its notice; another question cannot. Failed-refresh notices require successful
fresh verification rather than reuse of the old capture. Notices are session-only,
and final replies include notices for readers touched in the current request.

Existing runtime traces record model, elapsed seconds, attempts, actions,
page work, coverage, outcome and available provider token usage. No monetary
cost is inferred without a verified applicable price.

The accepted [spec](../.scratch/page-reader/spec.md) and
[ADR](adr/0003-isolate-webpage-reading.md) record the design and acceptance scope.
The [coverage specification](../.scratch/page-reader-coverage/spec.md) and
[ADR 0004](adr/0004-enforce-page-reader-coverage.md) record the search/fallback extension.
