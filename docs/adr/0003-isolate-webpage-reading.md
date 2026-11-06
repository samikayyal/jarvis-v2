# Isolate webpage reading from the main assistant

Design accepted and final alignment confirmed 2026-11-03. The design interview is complete; implementation is not yet authorized.

Delegate questions about individual webpages to a page reader, keeping full page content outside the main assistant's conversation. Basic document conversion preserves content, headings, tables, and link destinations; the reader model selects relevant evidence and returns concise answers with supporting excerpts, conditions, and unresolved gaps. The main assistant controls navigation between pages and synthesis across sources, trading an additional model call and possible summarization errors for focused reading and reduced main-context growth.

Every webpage read initially uses this boundary; compact search results remain with the main assistant for discovery. Readers receive only deliberately supplied task context and their own follow-up history, with no independent access to private tools. They may inspect and adjust public-page display controls within the assigned page; the main assistant owns cross-page navigation.

Documents below a size threshold are read whole; larger documents are split at headings and the reader chooses chunks from a heading index. The configurable threshold starts at 12,000 tokens, with paragraph/table-row splits for oversized sections and a plain size split only when structure is absent. Page content and reader conversations remain available during the current working session. Follow-ups reuse existing evidence unless an explicit recheck requests a refresh; no per-fetch time tracking is required.

`web_reader.model` selects the reader model independently of the main assistant's model. The operator selected `gpt-6-luna` with low reasoning on 2026-11-03. Readers use that model without automatic escalation or fallback to a different model; missing or invalid reader configuration produces a clear error. Transient technical failures permit one bounded retry; unresolved facts are reported with the available evidence and gaps. Provider availability and setting support must be checked during authorized implementation, rather than inferred from this design choice.

The main assistant uses `read_web_page(url, query)` to create a reader and `ask_page_reader(reader_id, query, refresh=false)` for follow-ups. Responses carry a concise answer, supporting evidence, gaps, and relevant links; creation also returns the reader ID.

A simple in-memory registry retains reader state for the working session; reset or restart clears it. Expired IDs return a clear error. Refresh keeps the ID but resets the evidence conversation and rebuilds the captured document and heading index, using a self-contained query supplied by the main assistant. This avoids mixing old evidence with new content without introducing persistent storage or version tracking.

Chunks selected by the reader stay in its model conversation for follow-ups; they are not silently evicted or summarized. Capacity checks reserve response space and return `context_limit` before an addition or model call would exceed capacity, preserving existing reader state. Main Jarvis can explicitly call `read_web_page` again for the same URL to create a new reader ID and fresh conversation using the same configured reader model and settings. The new reader receives the supplied query, not the previous reader's transcript. Refresh instead keeps the existing ID and replaces its document and evidence conversation only after a successful fetch. Failed refreshes preserve the previous evidence but explicitly fail fresh verification.

Reader calls run sequentially as ordinary tools within the existing runtime process. Cancellation stops active work and closes its browser resources, preserving completed reader evidence for the rest of the working session. Acceptance uses repeatable fixtures and a live GCP check, evaluating source fidelity, follow-ups, refresh, context isolation, failures, cost, and latency.

Per-call duration, model/tool rounds, and final-response length have configurable limits. Limit outcomes preserve selected chunks and return usable findings when available. The [ready-for-agent specification](../../.scratch/page-reader/spec.md) defines the initial limits: 120 seconds, eight model attempts, eight reader actions, a 64,000-token context budget with response reservation, and a 2,000-token returned result. Reader count and captured-document size also have explicit bounds. These starting values are tuned during implementation and acceptance testing; specification publication does not authorize implementation or deployment.

[ADR 0004](0004-enforce-page-reader-coverage.md) extends this design with mandatory
bounded fallback and measured coverage, and updates the reader defaults to 24
model turns, 18 actions and 128,000 context tokens while retaining 120 seconds.
