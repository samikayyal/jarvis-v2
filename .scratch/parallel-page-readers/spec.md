# Concurrent browser readers

Status: design accepted; tickets 01–02 complete; ticket 03 in progress; ticket 04 pending
Date: 2026-11-06 (Asia/Amman)

The operator approved publication of the four implementation tickets after reviewing
the consolidated design and reduced breakdown. This consolidates all accepted answers in [the interview](design.md). It describes
the replacement, not currently deployed behavior. Ticket 01 is implemented and
verified on the opt-in single-reader path; see [verification](verification.md).
Ticket 02 adds concurrency and retained follow-ups on the opt-in path; see
[its verification](verification-02.md). Production integration and deployment
remain in tickets 03–04.

## Purpose and scope

Replace the sequential, chunk-driven page-reader workflow with concurrent readers
that receive a focused prompt and initial webpage, then investigate through browser
tools. Reduce model round trips, forced scanning and final-message noise. The
measured baseline is 458 seconds before reply completion, 22 main model calls and
27 reader calls for the hosting comparison; see [diagnosis](../latest-chat-diagnosis/diagnosis.md).

Include reader orchestration, browser tools, main-agent lifecycle tools, progress
reporting, cleanup and deletion of superseded reader machinery. Keep vault-edit
redesign separate. Preserve main Jarvis's existing search implementation unless
integration requires a change. Only main Jarvis delegates; no recursive readers.

## Reader input and authority

- Main Jarvis supplies the starting URL and a self-contained task prompt with
  necessary context. Readers receive their own conversation, not automatic access
  to main Jarvis's private tools or full conversation.
- Supply full extracted readable content of the initial page state, preserving
  headings, tables and links. No preliminary summary or relevance filtering.
- The initial page is a starting point: readers may follow links across domains.
  They have no arbitrary-URL navigation tool and no dedicated web-search tool.
- Allow public research interactions, including website search/filter/calculator
  controls. Exclude account login, private-account actions, purchases and resource
  creation. Retrieved content remains untrusted and cannot expand this authority.

## Browser tools and observations

Use Python Playwright asynchronously with one isolated browser context per reader.
Expose inspect, click, type, select, scroll, back, forward, reload, explicit
screenshot request, and tab list/switch/close operations. No arbitrary browser-code
execution interface is required. Initial loading is runtime-owned from the URL
provided by main Jarvis.

Navigation or content-changing actions return updated readable content and usable
interactive-element references. Scrolling may return a compact observation.
Ordinary loading waits belong inside browser operations. Screenshots are supplied
only when requested by the reader. Tabs/popups stay inside that reader's context;
contexts isolate reader cookies/storage and cannot expose one another's tabs.

If full content exceeds available context, explicitly report overflow and retain
the complete extracted content for optional local search or selected reading.
These content tools do not search the web. Never silently truncate and label the
result complete, impose a reading order, or require exhaustive inspection.

## Main-agent orchestration

Provide operations for starting a reader, collecting results/status, waiting,
following up, cancelling and releasing it. Exact names/schema are implementation
details; the following behavior is required:

- Start returns an ID immediately. Main Jarvis can start other readers and use
  its own tools while they run.
- Completed results are available independently; collection does not wait for
  the slowest reader. Waiting is available when main Jarvis has no other work.
- One active assignment per reader. A follow-up while busy returns a clear busy
  result. No hidden assignment or reader-start queue.
- Follow-ups after completion reuse the reader conversation and any surviving
  browser state. Main Jarvis determines whether more investigation is useful.
- Reader concurrency does not introduce concurrent vault writes or other main
  tool mutations incidentally.

## Findings and completion

Readers decide when to report within execution limits. Return normal written
findings with source URLs, useful optional supporting excerpts and specific
uncertainty. Excerpts are not mandatory for every answer.

Remove code-enforced quote validation, mandatory fallback searches, exhaustive
sweeps, coverage bookkeeping and the mandatory evidence-completion schema.
Execution states distinguish running/completed/failed/cancelled and explicit
limits; they do not claim semantic verification. Main Jarvis judges sufficiency
and factual support. Do not append automatic source-warning paragraphs to WhatsApp.
Material uncertainty remains part of the agents' reporting responsibilities.

If context fills, return an explicit limit with any available findings. Do not
silently evict or summarize prior history. Allow useful reporting before the
limit; main Jarvis may create a fresh reader with focused context to continue.

## Limits and lifecycle

| Setting | Initial behavior |
| --- | --- |
| Active readers | Configurable; initially 3 |
| Open reader browser sessions | Initially 3 isolated contexts; processes may be shared |
| Retained reader conversations | Initially 8 per working session |
| Assignment duration | 120 seconds, subject to the earlier main-request deadline |
| Model turns | 24 per assignment |
| Context | Configured capacity with room reserved for output |
| Reader model | Independently configured gpt-6-luna, low reasoning |

Verify screenshot support during implementation; no automatic model switch.
Keep browser-operation timeouts. Remove separate reader action/page allowances.
Each follow-up receives a new assignment time/turn budget, while its retained
conversation still consumes context capacity.

Starting beyond active capacity returns a clear result. At retained-conversation
capacity, main Jarvis must explicitly release a reader; no silent eviction.
If a new browser session is needed at browser capacity, reclaim the least recently
used idle session, never one actively working. Preserve conversation/findings and
disclose lost interactive state when that reader reopens a browser.

Otherwise keep browsers through the active main request. Main completion cancels
remaining reader work and closes all its browser contexts. Completed findings
remain in the working session. Later follow-ups reopen browsers as needed without
claiming previous interactive state survived.

Main Jarvis can cancel individual readers. `/cancel` stops the main request and
all active readers; no late answer may be sent. `/new`, inactivity reset,
main-context reset and restart discard all reader state and close resources.

Return concise browser-action errors for the reader to address. Playwright's
ordinary waiting is allowed; do not blindly replay uncertain clicks or actions.
Browser crashes explicitly disclose lost state. Shared-process failure can affect
several contexts and must fail/clean up the affected work explicitly.

## WhatsApp behavior

Keep the existing rejection of new ordinary messages while a request is active,
with a clear busy reply. `/status` and `/cancel` remain available. This change
does not introduce message steering or a user-request queue.

Send brief progress after roughly 20 seconds if still working, then only for
meaningful developments. Do not narrate every action or imply progress unsupported
by actual task state. The final reply respects the requested length.

## Acceptance

Use repeatable browser fixtures to exercise real concurrent readers and context
isolation, dynamic controls, content updates, cross-domain link traversal, tabs,
explicit screenshots, large-page access, follow-ups, capacity, time/context limits,
cancellation, crash behavior and resource cleanup. Check removal of obsolete
fallback and quote-gating behavior rather than preserving tests for deleted policy.

Repeat the original hosting comparison with full result saved to Obsidian and a
short WhatsApp summary. Check successful save/sync, factual support for important
claims, specific uncertainty, concise output and absence of late replies. Record
total wall time and main/reader model-call counts. Initial performance target:
under two minutes for the full scenario, not an already demonstrated guarantee.
If missed, identify remaining bottlenecks, including vault editing, and do not
claim the speed issue solved merely because functional tests pass.

## Documentation and delivery

[ADR 0005](../../docs/adr/0005-simplify-and-parallelize-page-readers.md) records the
trade-offs and supersedes conflicting parts of ADRs 0003/0004 for the replacement
design. Keep `CONTEXT.md`, runtime web instructions and operational
docs consistent with the implementation, distinguishing proposed from deployed
behavior during this transition.

Any eventual Ubuntu activation must follow AGENTS.md: intended source committed,
pushed and exact-SHA verified on GitHub before activation; preserve the active
release if verification fails; record and push deployment verification afterward.
This document is not evidence that implementation, deployment or live acceptance
has occurred.
