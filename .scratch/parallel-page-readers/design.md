# Parallel page readers: design interview

Status: design accepted; ticket 01 complete; tickets 02–04 pending

The consolidated [specification](spec.md) is the controlling statement of the
proposed design. The rounds below preserve decision history; later answers refine
earlier wording.

## User-stated direction

- Simplify page-reader orchestration substantially.
- Main Jarvis can spawn multiple page readers concurrently.
- Each reader receives a prompt and a webpage, with full page content upfront.
- Each reader can operate a browser through Selenium or Playwright.

These establish the direction, not agreement on the detailed semantics below.

## Motivation

The latest hosting comparison took 458 seconds before reply completion, including
22 main model calls and 27 reader model calls. See
[the diagnosis](../latest-chat-diagnosis/diagnosis.md).

## Design tree

## Settled decisions — round 1

User response: "1 it can do whatever it wants, 3 no, yes to the rest".

1. A reader may independently follow links, navigate to other public sources and
   search beyond its starting page. The initial URL is a starting point, not a
   navigation restriction.
2. Supply full extracted readable content of the initial page state, preserving
   headings, tables and links, without prior summarization or relevance filtering.
   Oversized-page behavior remains open.
3. Remove code-enforced quote validation. Do not introduce a quote-repair gate as
   its replacement. The reporting format and evidence instructions remain open.
4. Browser authority covers public research: navigation, clicks, scrolling,
   selection, and public search/filter/calculator inputs. It excludes account
   login, private-account actions, purchases, and resource creation.
5. Readers decide when to return within runtime limits. Remove mandatory search
   rounds and exhaustive sweeps. Main Jarvis decides whether to follow up or use
   another reader.

## Settled decisions — round 2

User response: "7 it should have tools that allow it to do playwright-like
operations (click, scroll, etc...). yes to the rest".

6. Reader starts are asynchronous and return IDs immediately. Main Jarvis can
   start other readers, use its own tools, collect completed results independently,
   and wait when it has no other work.
7. Readers use general browser-action tools (navigate, inspect, click, type,
   select, scroll, etc.) with persistent browser state during active work.
   The requirement is Playwright-like operations, not a decision to switch the
   underlying engine from Selenium to Playwright or expose arbitrary code execution.
8. If full extracted content cannot fit, explicitly report overflow and keep the
   complete extracted content available for optional search/selected reading.
   No silent truncation, mandatory reading order, or exhaustive inspection.
9. Retain reader conversations for the working session. Keep browsers available
   until main Jarvis finishes the active request, then close them. Later reader
   follow-ups reuse findings and reopen a browser if needed, without claiming
   continuity of lost interactive state.
10. Return a normal written report with source URLs, useful optional supporting
    excerpts and specific uncertainty. No mandatory findings/coverage schema or
    binary evidence-completion verdict. Code tracks execution completion, failure
    and cancellation only. No automatic source-warning paragraphs in WhatsApp.
11. Only main Jarvis spawns readers; readers do not recursively delegate.

## Settled decisions — round 3

User response: "yes to all, except 13 no web search. it only gets the tools to
navigate the webpage it was given and i guess it can go anywhere it wants but not
explicitly. 14 it gets a tool to request a screenshot,".

12. Initially allow three concurrently active readers, configurable. Additional
    starts report capacity full; no hidden queue. Jarvis collects results before
    starting further work when capacity is full.
13. No reader web-search tool. Readers operate from the supplied webpage through
    browser tools. Following navigation beyond the starting page is allowed.
    Whether arbitrary URL navigation is exposed remains to clarify: "but not
    explicitly" narrows the earlier broad navigation direction.
14. Screenshots are supplied only through an explicit screenshot-request tool.
    Ordinary observation uses text and interactive-element information.
15. Jarvis can cancel individual readers. Finishing the active request cancels
    remaining reader work and closes its browsers. `/cancel` cancels the main
    request and all active readers. Completed findings survive in the working
    session; cancelled work cannot deliver a late answer.
16. Preserve existing busy handling for new ordinary WhatsApp messages while a
    request is active, with a clear busy reply and `/status` and `/cancel`
    available. No new request queue or incoming-message steering in this change.
17. Brief progress after roughly 20 seconds if still working, then only meaningful
    developments. No per-browser-action chatter; final replies respect requested
    length.

## Settled decisions — round 4

User accepted Q18–Q22 and requested Selenium/Playwright pros and cons before
deciding Q23.

18. Jarvis supplies the starting URL. Reader tools include click, type, select,
    scroll, back, forward, reload, inspect and screenshot. Readers may follow
    links across domains but receive no arbitrary-URL navigation tool and no
    dedicated web-search tool. This resolves and narrows the round-1 navigation
    wording; interacting with public website search controls remains allowed.
19. Per-assignment execution limits start at 120 seconds, 24 model turns and
    configured context capacity. Retain browser-operation timeouts and the main
    request deadline; remove separate reader action and page-count allowances.
    Follow-up assignments receive new assignment budgets.
20. Initially retain independently configured gpt-6-luna with low reasoning.
    Verify screenshot support during implementation; no automatic model switching.
21. Context exhaustion returns an explicit limit result with available findings,
    without silently dropping/summarizing history. Jarvis may start a fresh reader
    with focused context. Readers can report useful findings before exhaustion.
22. Cap open reader browsers at three as well. Reclaim the least recently used
    idle browser when a new reader needs a slot. Preserve its conversation and
    findings; explicitly disclose reopening and lost interactive state. Never
    evict an actively working browser. This is an exception to retaining all
    browsers until main-request completion.
23. User selected Playwright after comparing the alternatives. Use its async
    Python interface with one isolated browser context per reader. The three-open-
    browser limit means three reader browser sessions/contexts; browser processes
    may be shared. Leave main Jarvis's existing search implementation alone unless
    integration requires a change. Browser-process failure may affect multiple
    contexts and must produce explicit reader failures and cleanup.

## Settled decisions — final round

User accepted Q24–Q30 with "yes to all".

24. Navigation and content-changing actions return updated readable content and
    interactive-element references. Large results use explicit overflow handling.
    Scrolling may return a compact observation; screenshots are explicitly requested.
    Browser tools handle ordinary loading waits.
25. Allow tabs/popups inside the reader's isolated context, with list/switch/close
    tools. Tabs close with that context and cannot expose another reader's tabs.
26. One active assignment per reader. Main Jarvis may inspect status, wait or
    cancel while it runs. Follow-ups while busy return a clear busy result; no queue.
27. Retain up to eight conversations per working session initially. Main Jarvis
    can explicitly release a reader; capacity returns an error, not silent eviction.
    `/new`, inactivity reset, main-context reset and restart discard all readers.
28. Browser-action failures return concise errors for the reader to act on.
    Normal Playwright waiting is allowed; uncertain actions are not blindly
    replayed. Browser crashes explicitly disclose lost interactive state.
29. Acceptance requires repeatable browser tests and the original hosting-and-note
    scenario. Evaluate concurrency, dynamic controls, screenshots, follow-ups,
    overflow, cancellation, cleanup, concise replies and factual support. Target
    under two minutes for the full scenario; report measured latency/calls and
    remaining bottlenecks if the target is missed.
30. Scope includes reader replacement, main-agent lifecycle tools, progress and
    removal of superseded reader machinery. Vault-edit redesign is separate;
    benchmark the complete workflow so its remaining cost is visible.

## Remaining decision frontier

Empty. The operator moved the consolidated design to ticketing and approved the
reduced four-ticket breakdown. Ticket 01 is complete on the opt-in single-reader
path; tickets 02–04 remain ready-for-agent under this feature's issues directory.

## Documentation

Use this repository's `CONTEXT.md` as its domain glossary. Record agreed terms
there with an explicit distinction between the opt-in replacement and the
currently deployed runtime. Accepted ADR 0005 supersedes conflicting parts of
ADRs 0003/0004 for the replacement design; production integration is still pending.

All design decisions and the reduced ticket breakdown are agreed. Ticket 01 is
implemented and verified; see [verification](verification.md). Concurrency,
retained follow-ups, production integration and deployment remain in tickets 02–04.

## Verified implementation facts

Source inspection during the interview established that deployed captures create
and close a browser per operation, replaying display actions; retained readers
hold text/transcripts, not live browsers. Ticket 01 now provides persistent
Playwright browser ownership on the opt-in path. The deployed engine remains
Selenium/Chrome with narrowly listed display controls; the opt-in replacement
provides general browser operations and public research forms.
Main and reader loops currently reject multiple tool calls per model response.
Existing per-request allowances and reader limits cannot simply be reused without
deciding how they apply to concurrent, multi-page investigations.
