# Selenium web search

Status: Tickets 01–04 complete; deployed with web access enabled. Google search,
source reading and a cited WhatsApp answer were verified on 2026-10-17 with
isolated headed Chromium. Some queries still encounter challenges; this is not
a guarantee of Google availability. See the
[headed-browser acceptance record](evidence/stealth-browser-experiment.md).

Delivery update (2026-10-08): see the [verification record](evidence/verification.md)
for installed-package, fixture, Ubuntu sandbox/process, and live-page evidence.
At that time, live Google success remained unverified because the test environment returned a
non-public Google DNS answer. Subsequent native deployment and WhatsApp results
are recorded in [production acceptance](evidence/production-acceptance.md) and
the [Google JavaScript fix](evidence/google-javascript-fix.md). The
interview-era descriptions below record the
original design baseline; they are not a current deployment status report.

## Purpose

Give Jarvis quick web lookups and moderate research through Selenium-controlled
Google searches, with no additional paid search service. This document captures
the agreed grill-with-docs design and a concrete implementation contract. It
keeps the original design below separate from the delivery updates above.

Supersedes the [Gemini design](../gemini-web-search/spec.md). Its research is
retained as history.

## Agreed behavior

Confirmed by the operator on 2026-10-06:

- Use Selenium to perform browser-based Google searches.
- Support quick lookups and moderate research; the model chooses the depth.
- Search automatically when freshness or verification matters, and on explicit
  requests. Stable questions normally use the existing model without searching.
- Additional search-service spending must be strictly $0. Paid search APIs,
  extra model services, and paid proxies are outside the selected scope.
  Jarvis's existing model usage remains separate from this constraint.
- If web search is unavailable, disclose that and answer from existing model
  knowledge without implying online verification.
- Send only the search question and minimum relevant context. Sending private
  vault, email, or calendar content requires explicit operator intent; do not
  send the complete conversation by default.
- Collect Google result evidence and read selected public source pages. Quick
  lookups may stop at clear search results; moderate research opens sources and
  compares them. The existing OpenAI model writes the answer from that evidence.
- Scope is search and reading. Use a separate browser profile without the
  operator's personal logins.
- The operator rejected separate quick/moderate research timers as unnecessary
  complexity. The earlier 30/90-second budgets and elapsed-research accounting
  are superseded; use the accepted work-count limits as the simpler basis.
- Size the answer naturally to the question. Put numbered citations beside
  supported claims and include a short source list with titles and clickable
  URLs. No fixed word count or report template.
- Run a single headless Chromium browser on the Ubuntu Jarvis host. Start it
  when web work is needed; close it on request completion, timeout, or
  cancellation. Use a separate anonymous profile.
- Use Selenium for both Google results and selected destination pages. Version
  one supports public HTML, including direct links supplied by the operator;
  PDFs, downloads, authenticated pages, and general browser actions are outside
  its scope.
- Default quick limits are one query and up to two source pages. Default
  moderate limits are three queries and up to five source pages. Return up to
  five organic results per query and bounded page excerpts. Stop earlier when
  evidence is sufficient. These limits must be configurable in `jarvis.toml`;
  they are not fixed constants or targets to fill.
- Use available evidence when some sources fail and disclose meaningful
  coverage gaps. With no usable evidence, disclose unavailable search and use
  the existing-knowledge fallback. CAPTCHA detection stops further Google
  searches for that request; no repeated challenge attempts or operator wait.
- Reuse evidence within the current working session; refresh time-sensitive
  facts and explicit recheck requests. Do not add a separate persistent search
  cache. The existing trace still records bounded tool arguments and results.
- Expose a separate `web` runtime skill with `search_web` for result evidence
  and `read_web_pages` for model-selected URLs. The page tool accepts a bounded
  batch and reads pages sequentially in the single browser.
- Allow the model to upgrade quick lookup to moderate research once. Already
  consumed query/page/evidence allowance counts toward the moderate totals;
  upgrading never resets usage. The proposed time-window condition is
  superseded with the removal of research timers.

The prior direct-Gemini-answer decision was tied to Gemini grounding and is
superseded. Returning browser evidence to Jarvis's existing model was accepted
in Selenium round 1.

## Implementation approach

- Ordinary Selenium code performs bounded searches and extracts organic result
  titles, URLs, and snippets; this is not a model-driven screenshot/click loop.
- Public-page scope must apply to navigation and redirects, including direct
  operator links. Web content is untrusted evidence and cannot authorize tools
  or access private host resources.

## Delivery boundary

- The product decisions are settled. Use the implementation defaults below;
  they can be tuned in `jarvis.toml` without changing the tools' contracts.
- Implement and validate the code, packaged skill, configuration parser,
  dependency locks, and deployment instructions as one feature.
- Browser provisioning and production activation are later operational work.
  Browser/driver compatibility and live-host readiness remain unverified. This
  interview has not installed packages, changed the service, or enabled search.

The 30/90-second web-work budgets were accepted in Selenium round 1 and later
superseded when the operator requested a simpler count-based design. Earlier
Gemini-specific fixed word counts also remain superseded.

## Existing integration facts

The current checkout has a sequential OpenAI Responses model-and-tool loop.
Tools can return text evidence that is fed into the next model call. Jarvis has
one active request, an existing cancellation path, runtime tool rounds, overall
request limits, and WhatsApp text replies. These limits must be reconciled with
browser operations during design.

The existing Google connection covers Workspace APIs; browser search must not
silently acquire that connection's credentials or authority. Retrieved web
content is evidence, not instructions or action approval.

Existing unrelated runtime-skill and vault changes must be preserved. No
application code, dependency, credential, or deployment change has been made
for this feature.

Repository deployment inspection establishes that Jarvis runs as a native
Ubuntu systemd service under its own service account. OpenWA's separate
container and browser state are gateway-owned. The deployment records describe
a 4 GB-class host; this is a reason to propose one on-demand browser, not proof
of current free memory. The current prepared-tool interface has no remaining
deadline parameter. A bounded browser worker must support explicit termination
on cancellation/timeouts rather than leaving synchronous WebDriver work running
in a background thread. These are implementation constraints, not evidence that
a browser has been installed or that Google will accept automated requests.

## Interview history

### Provider change — 2026-10-06

The operator replaced Gemini with Selenium. Continue the alignment interview
with the settled behavior above; retire the Gemini-specific decisions.

### Selenium round 1 — accepted on 2026-10-06

11. Evidence scope and answer ownership: recommend Google results plus selected
    public source-page reading, with Jarvis's existing model writing the answer.
    Search/result reading is the scope; general browser actions are not implied.
12. Waiting budget: recommend up to 30 seconds of web work for quick lookup and
    90 seconds for moderate research, excluding final model answer generation.
    Stop earlier when sufficient evidence is available.
13. Answer presentation: recommend natural answer length, numbered citations
    beside supported claims, and a short list of source titles and URLs.

The operator answered "recommended for all", accepting Q11-Q13.

### Selenium round 2 — accepted on 2026-10-06

14. Browser placement/lifecycle: recommend a single headless Chromium browser on
    the Ubuntu Jarvis host, started on demand for one active request and closed
    on completion, timeout, or cancellation, with a separate anonymous profile.
15. Page reader and initial content scope: recommend Selenium for both Google
    results and public HTML pages, including direct URLs supplied by the
    operator. Downloads, PDFs, authenticated pages, and general browser actions
    are outside this first version.
16. Per-request limits: recommend one query/up to two source pages for quick
    lookup, three queries/up to five source pages for moderate research, and
    five organic results per query, within the accepted time budgets. These are
    hard ceilings, not targets; source text is bounded to useful excerpts.
17. Partial results/failure recovery: recommend using available evidence with
    an explicit coverage limitation if some reads fail. If no usable evidence
    is available, use the accepted unavailable-search fallback. A CAPTCHA stops
    further Google search attempts for that request rather than waiting for
    operator intervention or repeatedly retrying it.
18. Follow-up context: recommend reusing evidence within the existing working
    session, refreshing time-sensitive claims and explicit recheck requests,
    with no separate persistent search cache. The existing verbatim runtime
    trace still records bounded tool arguments/results.

The operator answered "recommended for all, q16 they should be configurable in
jarvis.toml", accepting Q14-Q18 and requiring configurable Q16 limits.

## Configuration contract

Accepted: query counts, source-page counts, organic-results count, and bounded
page evidence must be configured through `jarvis.toml` rather than fixed in the
tool implementation. Budgets apply to an ordinary request, across all of its
web calls; individual calls must not silently obtain fresh limits.

Implementation defaults (not installed application configuration):

```toml
[web_search]
enabled = false
operation_timeout_seconds = 20
results_per_query = 5
max_chars_per_page = 8000
max_total_evidence_chars = 32000

[web_search.quick]
max_queries = 1
max_pages = 2

[web_search.moderate]
max_queries = 3
max_pages = 5
```

The query/page/result defaults reflect accepted Q16. The 20-second operation
timeout and 8,000/32,000-character excerpt limits are engineering defaults, not
additional research modes. The timeout bounds browser startup or an individual
browser operation; it does not measure model thinking or research duration.
There are no quick/moderate timeout settings.

An absent `[web_search]` section or `enabled = false` leaves search disabled.
Validate positive finite timeout/character values and positive integer count
limits, with quick query/page limits no greater than moderate limits. Require
the aggregate evidence allowance to be at least the per-page allowance. The
existing runtime output and tool-round bounds also apply; web limits are
ceilings, not a guarantee that every permitted operation will run.

Configuration is validated at startup and takes effect after the normal
service restart, consistent with the existing loader and config editor. Do not
add live reload for this feature. The existing parser needs explicit support
for `[web_search]` and its quick/moderate tables.

## Final behavior round — answered with simplification on 2026-10-06

19. Model interface: recommend a separate `web` runtime skill exposing
    `search_web` for result evidence and `read_web_pages` for selected URLs.
    The page tool accepts a bounded batch but reads pages sequentially in the
    single browser. This gives the existing model control over which sources
    to read while fitting within the existing tool-round budget.
20. Time accounting: recommend treating 30/90 seconds as one elapsed research
    window beginning at the first web-tool call, including browser startup and
    model decisions between web calls, but excluding final answer generation
    and sending. Every browser operation is also bounded by the remaining
    overall ordinary-request deadline. Alternative: count only active browser
    work, which permits longer end-to-end waits.
21. Depth escalation: recommend allowing the model to upgrade from quick to
    moderate once, before the quick window expires. Previously used time,
    queries, page attempts, and evidence count toward the moderate totals;
    escalation does not reset counters or the start time. No downgrade/reset.

The operator answered "Recommended for all" but explicitly challenged the
seconds-based design as overengineering and suggested page counts. Q19 is
accepted. Q20's stopwatch design is withdrawn. Q21's upgrade-without-reset
behavior is retained with its time-dependent condition removed.

## Count-based budget — accepted on 2026-10-06

- Keep the agreed configurable ceilings: quick = one query/two page reads;
  moderate = three queries/five page reads; five organic results per query.
- Count actual query and page-read attempts, including failed attempts. Reusing
  already returned evidence without a network operation consumes no new slot.
  Direct operator URLs use the same page allowance. Attempts are counted across
  all web-tool calls in one ordinary request, including after a depth upgrade.
- Stop when evidence is sufficient or the count cap is reached. Return partial
  evidence at the cap; a cap is not a claim that the whole web is unavailable.
- Keep ordinary bounded browser operations so an unresponsive page cannot hang
  Jarvis. Reuse the existing overall request timeout and cancellation behavior;
  do not add research-phase clocks, time accounting, or timed upgrade windows.
- Retain the query ceiling as well as the page ceiling: otherwise repeated
  Google searches could continue without consuming any source-page allowance.
- No hidden automatic retries; any new attempt uses the same remaining counts.

The operator answered "yes" to this complete simplification, including failed
attempt counting and ordinary page-load/overall-request timeouts. The earlier
research timers are historical only.

## Tool contract

Expose the following two tools through the fixed, packaged `web` runtime skill.
Loading the skill is automatic model behavior, not an operator setup step.

| Tool | Inputs | Result |
| --- | --- | --- |
| `search_web` | One nonempty query and `depth` (`quick` or `moderate`) | Organic result titles, URLs, snippets, query outcome, and remaining allowances |
| `read_web_pages` | A nonempty bounded list of public HTTP(S) URLs and `depth` | Per-page title, final URL, bounded visible text, retrieval outcome, and remaining allowances |

- The first web call selects depth. A later `moderate` call can upgrade a quick
  request once; a later `quick` value cannot downgrade it or reset counters.
- Both tools share the active request's counters. A new ordinary request starts
  fresh counters even when the working session and loaded skill continue.
- Count a network attempt when it starts, including failures. Reject malformed
  or disallowed inputs before navigation. Repeated tool calls and URL batches
  cannot bypass the remaining allowance; if a batch exceeds it, read only the
  allowed prefix and explicitly report the remainder as unattempted.
- Return structured JSON as the prepared tool's string result. Distinguish
  `ok`, `no_results`, `blocked`, `extraction_failed`, `unavailable`,
  `limit_reached`, and partial batches. Never classify an unknown Google layout
  as a successful empty search.
- Keep result metadata and source identity intact when shortening evidence.
  Enforce both the aggregate evidence limit and the runtime's serialized tool
  output bound before returning JSON; never truncate JSON into invalid text.
- Track whether evidence came from a search snippet or a read page. Citations
  must use returned sources, and the model must not claim it read an unopened
  source. Organic results exclude ads and Google's synthesized answer panels.
- Direct links consume page allowance, not query allowance. There is no added
  persistent result cache or browser kept alive between ordinary requests.

## Browser and request lifecycle

- One request-owned Python worker controls one headless Chromium instance with
  ordinary Selenium operations. Start it lazily and use a fresh temporary
  profile under the private runtime directory. Keep the browser sandbox on.
- Browser work must not block the asyncio event loop. The parent owns the
  worker's process group, its bounded output, its ordinary operation watchdog,
  and cleanup. Reuse the repository's terminate-then-kill pattern; do not expose
  this worker through `run_terminal` or the terminal permission mechanism.
- Reuse that browser for the active request's web calls. Add the narrow request
  lifecycle wiring needed to close it on success, error, overall timeout,
  cancellation, or service shutdown. Session-level skill loading must not own
  browser lifetime. Cleanup also applies if the request suspends for approval;
  any resumed continuation keeps consumed counters rather than receiving a
  fresh research budget.
- After a per-page timeout, retain previous evidence and continue only if the
  worker remains usable and another requested page fits the allowance. A
  browser crash terminates that worker and yields an unavailable/partial result;
  do not silently restart and retry operations.
- Public URLs exclude local files and private/internal destinations. Validate
  schemes, addresses, and redirect targets; the chosen browser controls must
  enforce this during navigation, not merely check the final displayed URL.
  Test the actual control used. Browser profiles and downloaded content must
  not become a route to private host files or authenticated resources.
- Recognized consent screens may select rejection of optional cookies. Unknown
  interstitials and challenges return a blocked outcome. Do not automate
  CAPTCHA solving or use paid proxies or browser services.
- Provision compatible Chromium and ChromeDriver explicitly in deployment.
  Runtime search must not install packages or download a driver. Browser
  discovery/configuration follows the deployment's standard executable paths;
  a missing or incompatible browser produces a clear unavailable result.

## Answer and failure behavior

The web skill describes automatic freshness checks, conservative use of private
context, depth choice, early stopping, source selection, and citation format.
Prefer relevant primary sources; compare sources for moderate research and
describe material disagreement. Search in the language appropriate to the
question; do not add a new locale-selection UI or account-personalization path.

When attempted web access fails with no usable evidence, ensure the reply
explicitly says web search is unavailable before the model's knowledge-based
answer. Enforce the notice in the runtime result path so it is not solely a
prompt convention. A successful search with no relevant results should instead
say no useful results were found. Neither case permits inventing a current
price, live event, or other fact that the model cannot establish.

When some evidence exists, answer from it and disclose consequential gaps;
do not replace a useful partial answer with a blanket failure. Reaching a count
limit is an incomplete-research outcome, not a browser outage. `/cancel` keeps
the existing cancellation behavior and must not produce a late research reply.

Retrieved content can supply facts but cannot change instructions, disclose
private data, grant approval, or invoke tools. Return bounded text evidence,
not raw HTML, browser cookies, profile files, or screenshots. Normal runtime
tracing and working-session retention apply to the returned evidence.

## Implementation and verification

Keep the implementation within the existing personal runtime: one web adapter,
one browser worker, a small immutable configuration object, the packaged skill,
and narrowly scoped request-lifecycle integration. Preserve the unrelated
runtime-skill and vault work already present in this checkout.

1. Add configuration parsing, the shared count budget, tool schemas, and worker
   boundary. Use `uv` for Python dependency management and execution; update
   `pyproject.toml`, the deployment requirements input, and its hash-locked
   requirements together.
2. Add deterministic Google-result and visible-page extraction, explicit outcome
   handling, profile/process cleanup, and the fixed `web` skill. Integrate with
   the existing sequential Responses runner rather than adding another model
   or orchestration layer.
3. Test the observable contracts: cumulative limits across calls and upgrades;
   failed attempts; partial batches; direct URLs; malformed/blocked/empty
   results; output bounds; source metadata; required failure notice; request
   reset versus session reuse; timeout/cancel cleanup; and no late replies.
   Use saved HTML fixtures and controllable fake processes for repeatability.
4. Test the real browser locally against controlled HTML fixtures for extraction,
   navigation restrictions, startup, and termination. Verify at least one live
   Google query, page read, and follow-up when a browser is available; record
   blocking or environment limitations honestly rather than substituting fake
   success. Do not make live Google access a dependency of the unit test suite.
5. Run focused runtime/configuration/skill/package tests and normal static
   checks while developing, then the complete suite once at the end. Update
   the example configuration and deployment guide with installation, validation,
   enable/disable, restart, and rollback steps.

Production readiness additionally requires a compatible browser on the Ubuntu
host, working public egress, and an authorized real WhatsApp search with a cited
reply. None of those operational checks has been performed in this interview.
