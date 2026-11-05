# Conversational page reader

Status: design accepted; interview complete

Historical interview archive. The [published specification](spec.md) resolves the model, reasoning setting, initial limits, and test-boundary decisions; earlier open questions below are preserved as conversation history.

Final alignment confirmed by the operator on 2026-11-03.

Scope: agree the design and document decisions. Runtime implementation and deployment are not authorized by this interview.

## Agreed decisions

Round 1, accepted 2026-11-03:

1. Each page reader answers questions about one webpage. Main Jarvis decides which pages to visit and combines findings.
2. Basic conversion preserves page content and structure, including headings, tables, and link destinations. The reader model decides relevance; an arbitrary initial text prefix is not its sole evidence.
3. Answers are concise and include supporting source excerpts, material conditions, and unresolved gaps. Main Jarvis can ask follow-up questions and request more evidence.
4. Exactly one reader model is selected by a configuration field. There is no automatic escalation to another model. The concrete field name and model value are not yet chosen.

Round 2, accepted 2026-11-03:

5. Readers may perform limited interactions within their assigned public page, such as expanding sections, selecting a region, switching pricing tabs, and loading dynamic content. Navigation to another webpage remains the main assistant's responsibility. Login, purchases, and submitting user data are outside this scope.
6. Large documents should use simple splitting. The user explicitly prefers simplicity over an elaborate outline/section retrieval design. Chunk processing order, coverage, and size remain to be agreed.
7. Readers receive only the assigned page, focused query, context deliberately supplied by the main assistant, and their own follow-up history. They do not inherit the main conversation or independently access the vault, terminal, Gmail, or other readers.
8. Every webpage read goes through a reader initially, including short pages. Compact search results remain available to the main assistant for discovery. A direct-reading shortcut is deferred until measurements justify it.

Round 3, accepted 2026-11-03:

9. Read documents whole below a size threshold; split larger documents at headings. The original sequential-processing proposal was superseded by decision 14: the reader selects chunks from a heading index. Preserve table context where practical and report actual inspection coverage.
10. Retain captured page content and the reader conversation within the current Jarvis working session, cleaning up when the session ends. Keep the browser running only while needed. Keep this lifecycle simple.
11. Ordinary follow-ups reuse existing evidence; explicit rechecks refresh it. Examples: "Explain that price" reuses evidence; "check the price again now" refreshes it. The user rejected per-fetch time tracking as unnecessary; no timestamp-based freshness or version-tracking subsystem is required.
12. Allow one bounded retry for a transient technical failure using the same configured model. Unresolved facts return available evidence, the specific gap, and useful links. Do not repeatedly retry ambiguity or invent an answer.

Round 4, accepted 2026-11-03, including annotation correction:

13. Use a configurable chunk threshold, initially 12,000 tokens. Group adjacent sections within it; split an oversized section at paragraphs or table rows, repeating headings/table headers as practical. Fall back to a plain size split when structure is absent. Validate the starting threshold during implementation.
14. Correction to decision 9: for large pages, show the reader a heading index and let it choose which chunks to inspect. Do not automatically process every chunk sequentially. The complete captured document remains available outside the model transcript. Report the actual inspection scope; absence from selected chunks does not establish absence from the page.
15. Main-agent interface: `read_web_page(url, query)` creates a reader and returns its ID and findings; `ask_page_reader(reader_id, query, refresh=false)` asks a follow-up. Findings include answer, evidence, gaps, and relevant links. Refresh replaces retained page content; prior claims are not newly verified merely by refreshing.
16. `web_reader.model` selects the reader model independently of the main assistant's `/model`. Missing or invalid reader configuration produces a clear error, not a model fallback. The actual model value remains open.

Round 5, accepted 2026-11-03:

17. Keep a simple in-memory reader registry tied to the working session. Session reset or restart clears it. An expired reader ID produces a clear error, allowing the main assistant to reopen the page. No database or separate reader service.
18. Refresh keeps the reader ID but resets its evidence conversation, fetches the page, and rebuilds its heading index. The main assistant supplies a self-contained query with the necessary context. Old chunk evidence must not be mixed with the refreshed document; refresh failure handling remains to be specified.

Round 6, accepted 2026-11-03, with correction to Q19:

19. Reader calls execute sequentially as normal tools inside the existing runtime process. The main assistant waits for the result; no background-job system. `/cancel` stops active reader work and closes active browser resources while preserving completed reader evidence until session end.
20. Selected chunks remain in the reader's model conversation for follow-up questions. The user rejected removing older chunk bodies from model input. Do not silently evict or summarize selected evidence. Behavior at context capacity and separate operational limits remain open; acceptance of the other questions does not imply acceptance of every part of Q19.
21. Refresh replaces the captured document and clears the old evidence conversation only after successful fetching. If fetching fails, report refresh failure and preserve old evidence for ordinary follow-ups; do not present it as the result of a fresh verification.
22. Acceptance covers GCP source/section discovery and pricing conditions; follow-ups retrieving previously omitted conditions without refetching; refresh using changed content without presenting stale prices as fresh; model-selected chunks with full page content kept out of the main transcript; and clear missing-fact, expired-ID, cancellation, and limit outcomes. Use repeatable page fixtures plus a live GCP check, measuring cost and latency alongside correctness.

Round 7, accepted 2026-11-03:

23. Check reader context capacity before adding chunks or making model calls, reserving space for the response. If capacity would be exceeded, preserve the reader and return a clear `context_limit` result. Do not automatically compact, remove chunks, or switch models.
24. Main Jarvis can explicitly create a fresh reader using `read_web_page(url, query)`, including for a URL already assigned to another reader. Each creation returns a new reader ID with a fresh conversation and uses the configured `web_reader.model` and reader settings. It does not inherit the old reader's transcript or selected chunks. The main assistant supplies the needed context in the new query. Existing readers remain available within their session lifecycle.
25. Each reader call has configurable duration, model/tool-round, and final-response-length limits. Hitting a limit returns a clear outcome with usable findings where available, preserving previously selected chunks. Initial operational values are to be tuned during implementation and acceptance testing.

## Design tree

- Role and authority: page scope agreed.
  - Limited public-page interactions: agreed; cross-page navigation stays with main Jarvis.
  - Compact search results stay with main Jarvis; every webpage read delegates: agreed.
- Source access: content-preserving conversion and semantic relevance agreed.
  - Oversized documents: heading index with model-selected chunks; configurable 12,000-token starting threshold and simple oversized-section fallback agreed.
  - Freshness: reuse existing evidence unless explicitly refreshed; no per-fetch time tracking.
- Communication: focused questions and evidence-backed answers agreed.
  - Reader context isolation and lack of private-tool access: agreed.
  - Lifetime: current working session; evidence-backed follow-ups agreed.
  - Two-tool interface and reader ID: agreed; in-memory registry and refresh reset agreed.
  - Refresh failure handling: preserve old evidence and report failure; replace only after successful fetch.
  - Selected chunks stay in reader context; capacity returns `context_limit` and permits explicit fresh-reader creation.
  - Evidence excerpts, conditions, gaps, links, and configurable response-length bounds: agreed; exact serialization is an implementation detail.
- Model policy: single configured model agreed.
  - Failure behavior: one transient-failure retry with the same model; otherwise report gaps.
  - Independent `web_reader.model` field: agreed; model value and reasoning settings open.
  - Sequential execution and cancellation: agreed.
  - Configurable per-call duration, rounds, and reply limits: agreed; initial values to be tested.
- Acceptance cases: agreed; implementation and rollout authorization remain separate from this interview.

## Remaining before implementation

- Explicit reader model value and any reasoning setting, plus tested initial operational limits. These remain configuration choices rather than silently assumed defaults.
- Implementation authorization; no runtime implementation or deployment is authorized by final design alignment alone.

## References

- Domain term: root CONTEXT.md, Page reader.
- Architecture: docs/adr/0003-isolate-webpage-reading.md.
- Motivating incident: .scratch/pricing-research-diagnosis/code-research.md.
