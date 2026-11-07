# Main web search and concurrent readers

The normal web skill combines the existing Selenium-backed Google organic search
with concurrent Playwright page readers. Search discovery stays in the main
assistant; full page investigation stays in isolated reader conversations. The
main Responses loop executes tools sequentially while delegated readers work
concurrently. OpenWA remains the messaging gateway.

## Search contract

`search_web(query, depth)` accepts one nonempty query (maximum 2,048 characters)
and quick/moderate depth. The first call selects quick by default; moderate
upgrades the allowance without resetting consumed usage. Later quick calls cannot
downgrade it. Defaults are one quick query or three moderate queries and five
results per query. Every network attempt, including failures, consumes a query.
Returned evidence contains title, public URL, snippet and `search_result`
provenance. A snippet is not an actually read source page.

Search uses the existing per-request snippet limit (`max_chars_per_page`, default
8,000) and total returned text allowance (`max_total_evidence_chars`, default
48,000). Source identity is retained intact when serialized output fits; omitted
sources/text do not spend evidence allowance. JSON output stays bounded by the
runtime output limit. Results report remaining query/text allowances and precise
failure or limit outcomes. No_results, blocking, extraction failure, unavailable
workers and allowance exhaustion remain distinct. Quick-to-moderate upgrades and
approval continuations preserve consumption. New ordinary requests reset usage.
Legacy quick/moderate max_pages settings remain accepted, but do not constrain
reader navigation or introduce a reader page counter.

Google challenges or CAPTCHA stop further searches for the request; there is no
challenge-solving, uncertain retry or claim that every block is an IP-reputation
problem. A known direct public URL can still be investigated by a reader after
search blocking. Useful partial evidence remains available in the working
session. Private material is sent to search or readers only with explicit
operator intent and minimum necessary context.

## Browser and request ownership

Search keeps its existing BrowserWorker/Selenium implementation and explicitly
provisioned Chromium/ChromeDriver pair. It owns an anonymous profile and private
worker for the request. Headless remains default; optional headed search uses
private Xvfb and does not control Playwright readers. The Chromium sandbox,
public URL/DNS/redirect proxy, fresh HOME/profile, download restrictions, Google
script policy and challenge checks remain. Missing binaries fail explicitly;
the runtime never installs a browser or driver during a request.

Completion, suspension, failure, timeout and cancellation close search workers;
cleanup is drained before a new request can acquire resources. No operator
browser profile, Workspace token or OpenWA pairing state is reused. Direct page
reading is handled by [concurrent readers](page-reader.md), which have their own
assignment, browser-context and conversation limits. They retain browser actions
and state within the active request and close browsers afterward.

## Discovery, progress and answers

The web skill exposes search and all six reader lifecycle tools only after
loading. Loading does not grant approval or reset budgets. Main Jarvis supplies
self-contained reader tasks, does other main work, collects independent results,
follows up when useful and releases unneeded conversations. Reader findings are
ordinary reports with URLs and uncertainty; execution states do not establish
semantic verification. Main Jarvis decides whether sources support its answer.

WhatsApp progress starts after roughly 20 seconds of continued processing, then
only on observable reader-state developments. It is scoped to the owning active
request, suppressed while awaiting approval and stopped before final/cancel
responses. Existing busy rejection and deterministic status/cancel remain.
Answers preserve the model's requested length, citations and ordinary-language
uncertainty. No code-owned warning paragraphs, quote gates, fallback scans or
coverage notices are appended.

## Verification and operations

Use `uv run pytest tests/personal_runtime` and the lint/format commands in the
root README. Real browser fixtures test dynamic controls, public forms, source
navigation, screenshots, overflow, follow-ups, capacity, limits, cancellation and
cleanup without model billing. Main-search fixtures check organic extraction,
network protections, output bounds and search budgets independently.

[The operations guide](../deployment/personal-runtime/README.md) covers browser
provisioning and configuration checks. The opt-in installed-wheel `verify-web.py`
probe uses scripted providers and live browser access; it does not send WhatsApp
messages or establish model accuracy. Reader-model screenshot capability has a
separate explicitly authorized opt-in fixture in [the Playwright runbook](playwright-reader.md).

Trace events record each main Responses request/output, reader assignment and
request/output, browser actions, elapsed times, final assignment state and model
turns. IDs distinguish concurrent readers and retained follow-ups. Count main
`responses_request` and reader `playwright_reader_request` events for the live
comparison; measure wall time from request admission to final outbound reply.
The saved-source baseline was 458 seconds, 22 main calls and 27 reader calls;
meeting the under-two-minute target still requires ticket 04 live verification.
No monetary cost is inferred without applicable verified pricing.

See [ADR 0005](adr/0005-simplify-and-parallelize-page-readers.md) and
[ticket 03 verification](../.scratch/parallel-page-readers/verification-03.md).
These documents describe the source implementation, not Ubuntu activation or a
successful live hosting-and-vault-save acceptance test.
