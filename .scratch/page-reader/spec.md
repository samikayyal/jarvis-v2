# Conversational page reader

Status: implemented; live WhatsApp acceptance pending

Design accepted and specification completed: 2026-11-03 (Asia/Amman).

This publishes the agreed design and initial limits. Implementation and deployment
were separately authorized on 2026-11-04; see [verification](deployment-verification.md)
for the deployed candidate and remaining live acceptance gate.

## Problem Statement

Jarvis can find a public webpage yet fail to answer a focused question because its current reader returns a short initial body excerpt. In the motivating GCP pricing investigation, the assistant reached an overview rather than the relevant machine-family table, lost useful link destinations, and exhausted page attempts while the needed rows lay beyond the returned prefix. Increasing a timeout or rereading the same prefix does not fix that failure.

The operator needs accurate, inspectable findings with source excerpts and pricing conditions, follow-up access to omitted details, and explicit rechecks. Large webpages should not fill the main assistant's conversation, and a concise answer must not conceal incomplete inspection or missing evidence.

## Solution

Delegate every webpage read to a page reader responsible for one assigned page. Main Jarvis retains search, chooses pages, asks focused questions, follows useful links, and combines findings. Each reader uses its own configured model and isolated conversation, reads small documents whole, and chooses chunks of large documents from a heading index.

Retain the captured document and reader conversation in the current working session. Selected chunks stay in the reader's context for follow-ups. Ordinary questions reuse that evidence; explicit refresh fetches again and replaces the evidence only on successful capture. Main Jarvis can create another reader with a fresh conversation when needed.

## User Stories

1. As the authorized operator, I want Jarvis to reach the relevant official pricing section, so that its answer rests on the correct source.
2. As the authorized operator, I want region, machine type, currency, billing units, purchase option, and exclusions preserved, so that prices remain comparable.
3. As the authorized operator, I want supporting excerpts and source locations, so that I can check important claims.
4. As the authorized operator, I want missing facts and partial inspection disclosed, so that a confident summary does not imply complete verification.
5. As the main assistant, I want compact search results for discovery, so that I can choose the pages needed for the question.
6. As the main assistant, I want to delegate one page with a focused query and necessary context, so that the reader can answer without inheriting my full conversation.
7. As the main assistant, I want a reader ID returned with findings, so that I can ask that reader follow-up questions.
8. As the main assistant, I want relevant link destinations in reader findings, so that I can decide which other pages to open.
9. As a page reader, I want content-preserving conversion of headings, tables, and links, so that extraction does not decide relevance through a prefix cutoff.
10. As a page reader, I want small documents available whole, so that simple reading requires no chunk-selection ceremony.
11. As a page reader, I want a heading index for large documents, so that I can select useful chunks rather than process everything sequentially.
12. As a page reader, I want oversized sections split with their headings and table headers, so that a row retains its meaning.
13. As a page reader, I want chosen chunks retained across follow-ups, so that I can reason from the evidence already inspected.
14. As a page reader, I want to request an unread chunk during a follow-up, so that I can investigate a condition omitted from my first answer.
15. As the authorized operator, I want ordinary follow-ups answered without refetching, so that explanations reuse the existing evidence.
16. As the authorized operator, I want an explicit recheck to fetch new content, so that old evidence is not presented as current verification.
17. As the main assistant, I want successful refresh to keep the reader ID but clear old evidence history, so that old and new claims are not mixed.
18. As the main assistant, I want failed refresh to report failure and preserve old evidence for ordinary follow-ups, so that a technical failure does not destroy useful prior work.
19. As the main assistant, I want to create a fresh reader for the same URL, so that I can pursue a narrower question without inheriting a full conversation.
20. As the authorized operator, I want reader state discarded when the working session ends, so that it follows Jarvis's existing session lifecycle.
21. As the main assistant, I want expired reader IDs to produce clear outcomes, so that I can reopen the page deliberately.
22. As the authorized operator, I want one configured reader model independent of the main model, so that model choice remains predictable.
23. As the authorized operator, I want missing or invalid configuration reported without model fallback, so that Jarvis does not silently change that choice.
24. As a page reader, I want limited public-page display interactions, so that I can expose relevant dynamic content without gaining broader browsing authority.
25. As the authorized operator, I want readers isolated from private tools and other readers, so that webpage content cannot obtain unrelated access.
26. As the authorized operator, I want cancellation to stop active reader work and close browser resources, so that abandoned work cannot produce a late answer.
27. As the main assistant, I want context and execution limits reported explicitly with usable findings, so that I can choose the next step without losing retained chunks.
28. As the authorized operator, I want only one bounded retry for a transient failure, so that ambiguity does not cause repeated spending.
29. As the maintainer, I want repeatable correctness tests and a live GCP acceptance case, so that polished wording alone cannot establish success.
30. As the maintainer, I want latency and token usage measured alongside quality, so that starting limits can be tuned from evidence.

## Implementation Decisions

### Ownership and interfaces

- Introduce a page-reader component within the existing personal assistant runtime, using the current model client, prepared-tool integration, cancellation, and working-session lifecycle. Keep the registry in memory; no separate service, database, or background-job framework.
- The main assistant uses `read_web_page(url, query)` to create a reader and `ask_page_reader(reader_id, query, refresh=false)` for follow-ups. The query includes all deliberately supplied task context. Model selection is configuration, never a tool argument.
- Creation returns a new reader ID plus findings. Both tools return an outcome, concise answer, supporting evidence, unresolved gaps, relevant links, and actual inspection coverage. Exact serialization is an implementation detail; preserve valid structured results and source identity under output limits.
- Evidence identifies the requested/final source URL and a useful heading or chunk location, quotes supporting content, and retains material table labels and conditions. Coverage distinguishes inspected chunks from the captured document and discloses incomplete capture. Finding no fact in selected chunks does not establish its absence from the page.
- Search remains available to the main assistant. Replace the exposed raw webpage-reading path with the two reader tools; every page read, including short pages, initially uses a reader. Update web-skill guidance and tool visibility together so the old raw tool cannot bypass the new boundary.
- Readers receive only their page, focused query, explicitly supplied context, and their own history. They cannot call vault, terminal, Google account, MCP, search, or another reader's tools. Public content is evidence, never authority or trusted instructions.
- The main assistant owns navigation to another webpage. Readers return useful links for it to follow. Existing safe redirect handling remains part of fetching the assigned URL.

### Capture, selection, and retained context

- Preserve page content, headings, table structure, and link destinations through basic conversion. Do not run the old 8,000-character body-prefix output limit before chunk selection. Separate capture limits from the compact findings budget.
- Documents up to 12,000 tokens are supplied whole. Larger documents stay outside the model transcript and expose a heading index with addressable chunks. The reader selects chunks through a reader-only operation such as `read_page_chunk`.
- Group adjacent sections up to the configurable chunk threshold. Split an oversized section at paragraphs or table rows, repeating headings and table headers where practical. If structure is absent, use a plain size split. Preserve link destinations and disclose any incomplete capture.
- Track inspected chunks from successful tool results. Retain their full bodies, along with reader questions, answers, and tool exchanges, for later calls. Unread chunks remain outside the model context. Do not silently compact, summarize, evict, or replace selected evidence to make room.
- Check capacity before adding a query, index, chunk, tool result, or making a model call. Include instructions, exposed reader tools, and retained transcript, and reserve the next model response allowance. Return `context_limit` before exceeding the configured or supported model capacity, preserving existing reader state.
- Limited interactions may expand sections, select a region, switch pricing tabs, or load content within the assigned public page. Preserve labels describing the selected display state alongside resulting evidence; an earlier region's row must not silently become evidence for another region. Selected evidence remains retained. Explicit refresh is the mechanism for discarding old evidence history.
- Preserve public URL/DNS/redirect protections, anonymous browser isolation, restricted worker access, download restrictions, challenge stopping, and worker cleanup. Login, credential entry, purchases, and submitting user data remain excluded; a public page merely containing a login widget is not automatically excluded.

### Follow-ups and lifecycle

- Ordinary follow-ups reuse the captured page and retained conversation. They may inspect additional retained chunks without a network fetch. Any deliberately requested display interaction must be reported as additional page work, rather than described as evidence-only reuse.
- Refresh keeps the reader ID. Capture and prepare the replacement document successfully before replacing retained content and clearing the previous evidence conversation. The main assistant supplies a self-contained refresh query. On fetch/capture failure, return `refresh_failed`; preserve prior state for ordinary follow-ups but do not use it as the fresh-check result.
- If replacement capture succeeds but subsequent reader inference fails, report that failure; never restore old claims as fresh findings. No timestamp-based freshness system or document-version history is needed.
- Calling `read_web_page` again for the same URL creates a distinct reader with a fresh fetch and conversation under the same configuration. It does not inherit another reader's transcript or chunks. Existing readers remain available until the working session ends.
- Clear the registry on every working-session end: `/new`, inactivity reset, main-session context reset, and service restart. Invalid or expired IDs produce a clear error rather than reading another reader or silently recreating one.
- Execute reader calls sequentially as ordinary tools. `/cancel` interrupts active model/browser work, closes resources, and suppresses late replies; completed reader evidence remains until session end. Browser lifetime is limited to active work; retained evidence does not require a permanently running browser.

### Model and initial limits

The operator selected `gpt-6-luna` with low reasoning and delegated selection of reasonable initial limits. These are configuration decisions, not claims of verified provider availability or measured optimal settings.

| Setting or allowance | Starting value | Meaning |
| --- | --- | --- |
| `web_reader.model` | `gpt-6-luna` | Single reader model, independent of main-assistant `/model` |
| Reader reasoning effort | `low` | Explicit reader setting |
| Whole-document/chunk threshold | 12,000 tokens | Maximum normal chunk size, including repeated structural context |
| Reader context budget | 64,000 tokens | Entire model request plus reserved response space; also respect actual model capacity |
| Invocation deadline | 120 seconds | Total time for one create, follow-up, or refresh, including fetches, model calls, and retry |
| Model-call attempts | 8 per invocation | Includes any transient-failure retry |
| Reader tool actions | 8 per invocation | Shared across chunk reads and public-page interactions, including failed attempts |
| Returned findings | 2,000 tokens | Total compact result to the main assistant, including envelope and evidence |
| Transient retry | 1 per invocation | Same model; only if deadline and attempt allowances remain |
| Retained readers | 8 per working session | Reject further creation clearly; do not silently evict existing readers |
| Converted capture | 1,000,000 characters per reader | Bound retained page content and report incomplete capture explicitly |

- The first six execution/output values carry forward the interrupted specification's proposal. The registry and capture limits bound session memory while retaining the selected-chunk contract; validate these initial values against the large-page fixture and GCP acceptance case.
- The returned-findings cap is separate from the provider's generation allowance. Set a bounded provider output allowance compatible with the chosen model and its reasoning setting; include it in capacity reservation. Do not assume 2,000 returned tokens alone covers internal generation. Reject unsupported model/settings clearly and do not substitute another model.
- Reader invocation limits reset on each explicit call; retained context and reader-count limits do not. A fresh reader cannot reset the active request's search/page allowances. Local chunk inspection does not consume a page-fetch allowance; new captures and refreshed/rendered network work remain subject to the existing request-owned public-web limits.
- Outer request deadlines and existing browser-operation timeouts still apply: the earliest deadline wins. Retries cannot extend a deadline, bypass a page allowance, or repeat uncertain side effects. Retry transient technical failure once; do not retry missing facts, invalid settings, context exhaustion, access challenges, or ambiguity.
- Return distinct cancellation, context-limit, time/attempt-limit, reader-capacity, configuration, expired-ID, and refresh-failure outcomes. Retain usable findings and evidence when available. Do not make an extra unbudgeted model call to compose an error result.
- Record reader model, elapsed time, attempts, actions, coverage, outcome, and available token usage through existing tracing. Estimate cost only with a verified applicable price; otherwise report usage without inventing cost. Avoid a new analytics subsystem.

## Testing Decisions

The operator explicitly approved the existing runtime/tool integration boundary with fixture webpages and simulated model responses, plus a live GCP check and a few document-splitting edge cases. That approval was recovered from the interrupted chat and does not need to be requested again.

- Prefer tests through the real personal-runtime/model-and-tool loop and prepared web tools, substituting browser content and model responses at the existing external boundaries. Exercise the main assistant and reader independently so their inputs and exposed tools can be checked.
- Existing web-response tests provide prior art for loaded-skill visibility, ordinary evidence reuse, explicit rechecks, session resets, cumulative allowances, cancellation/timeout cleanup, and output fitting. Extend those behavioral seams instead of inventing a new test framework or asserting private registry layouts.
- Verify that the main transcript receives compact findings and reader IDs, while full page bodies and selected chunks appear only in reader inputs. Verify that private tools, main history, and other readers' evidence are absent from reader requests.
- Use a pricing overview fixture linking to a large machine-family document with relevant rows beyond the former prefix. Require the main assistant to follow the returned link; assert exact region, machine, currency, units, purchase conditions, and exclusions in evidence.
- Test small-document reading; heading-selected chunks; paragraph/table-row fallback; a heading-free document; repeated table headers; and accurate partial coverage. Keep narrow converter tests only where they explain behavior awkward to cover at the runtime boundary.
- Test a follow-up about an already selected chunk and a follow-up requiring an unread chunk. Neither should refetch. Assert that previously selected chunk bodies remain in subsequent reader input.
- Test successful refresh with changed content, failed refresh preserving old state without fresh claims, inference failure after successful replacement, and fresh-reader creation for the same URL with no inherited history.
- Test same-page display changes with distinct region labels, rejected cross-page interaction, untrusted webpage instructions, public login-widget content, and existing URL/network/challenge controls.
- Test every working-session reset, invalid IDs, cancellation during capture and inference, no late answer, and browser cleanup before the next request. Verify completed readers survive cancellation but not session reset.
- Test context checks before additions and model calls, output reservation, the eighth/ninth model/action boundary, retry accounting, earlier outer deadlines, output fitting that remains valid structured data, reader-count exhaustion, and capture-size disclosure. Limits must not discard selected chunks or reset shared request allowances.
- Validate missing/invalid reader configuration and prove main `/model` does not alter reader selection. Model availability and low-reasoning support require a real configured-provider check during authorized implementation/acceptance; fixture success alone is insufficient.
- Run focused and applicable full repository checks during implementation. For eventual live acceptance, exercise the GCP discovery and pricing case, evidence-only follow-up, explicit recheck, and cancellation through Jarvis WhatsApp. Check cited source fidelity and capture usage/latency; finish with idle `/status` and clean browser resources. Live web content may change, so compare against inspected evidence rather than hard-coding today's price.

## Out of Scope

- Runtime implementation, deployment, or live acceptance as part of this specification-writing task.
- Autonomous research by readers, cross-page navigation, reader-to-reader delegation, or private-tool access.
- Automatic model fallback/escalation, automatic chunk eviction or summarization, and automatic fresh-reader creation hidden from the main assistant.
- Persistent reader storage, restoration after restart, document-version history, per-fetch timestamp policy, background jobs, concurrency, embeddings, or a vector database.
- Login, purchases, submitting user data, or broad browser automation beyond assigned public-page display controls.
- A raw-read shortcut for short pages, unrelated full-access changes, gateway changes, and broader web-search redesign.

## Further Notes

The heading-index approach supersedes the earlier sequential-all-chunks proposal. Retaining selected chunks supersedes the rejected proposal to drop older chunk bodies. Historical statements that model selection or refresh handling were still open are resolved by this specification.

Initial limits are starting settings, not measured performance guarantees. Implementation may tune them from the agreed acceptance evidence while preserving the accepted semantics. Deployment, once authorized, must follow the repository's GitHub synchronization rule: commit and push the intended candidate, verify its exact remote SHA, then activate that source and record the results.

Related records: [design interview archive](design-notes.md), [page-reader ADR](../../docs/adr/0003-isolate-webpage-reading.md), [domain vocabulary](../../CONTEXT.md), and [motivating investigation](../pricing-research-diagnosis/code-research.md).

## Comments

- 2026-11-03: Recovered the latest accepted design, explicit model/reasoning choice, and approved test boundaries from chat `01a193ca-0b1c-7ce1-8e7d-410ade98fbc4`. Its specification turn ended with an upstream timeout after preserving the design notes. Completed the local Markdown specification here; runtime and deployment remain unchanged by this task.
