# Pricing-research failure: code and source findings

Investigation date: 2026-11-02 (Asia/Amman)

This is a read-only diagnosis. The sanitized incident evidence is
`.scratch/pricing-research-diagnosis/web-trace.json`; the deployed-source snapshot
used for this review is `.scratch/pricing-research-diagnosis/live-source/`.
The snapshot was normalized against the working-tree web files and is the source
of the line references below. No runtime or production files were changed.

## What happened in the 2026-10-31 request

The trace shows a research-strategy failure, followed by a hard tool limit—not a
generic browser failure:

1. Jarvis spent all three moderate search attempts (20:44:17, 20:45:05,
   20:45:12 UTC). The first and third searches returned five and five mostly
   non-official/opaque Google `/goto?url=...` result links; the second returned
   `no_results`. None yielded the official machine-family price table.
2. It then read four URLs in one static batch: `compute/all-pricing`, regions,
   disks, and networking. The first URL redirected to
   `https://cloud.google.com/products/compute/pricing`, the general overview.
   All four reads were successful only as 8,000-character `body_prefix` excerpts
   (`source_chars` 20,697; 52,037; 38,178; and 119,931). The trace reports
   `page_text_limit` for each.
3. It spent the last page attempt rereading `https://cloud.google.com/compute/pricing`,
   which redirected to the same overview and returned the same 8,000-character
   prefix. At that point the request had `pages: 0` and `evidence_chars: 6,137`.
4. There was no rendered read in this incident. The request ended with an honest
   “could not verify current E2 rate rows” result because the model never read the
   specific general-purpose pricing page and the available excerpts did not contain
   the E2 rows.

The first search's result URLs were opaque Google `/goto?url=...` tokens. The
search parser deliberately preserves those tokens rather than guessing a hidden
destination; reading one would have consumed a page attempt. The model instead
selected guessed/legacy direct URLs. This is why the incident is best described as
“source discovery did not reach the right official page, then prefix extraction and
allowance limits prevented recovery,” rather than “Google pricing was unavailable.”

## Why the returned page did not contain the price

The official overview at `https://cloud.google.com/products/compute/pricing`
contains navigation and a “General-purpose — View prices” link, but its visible
body prefix does not contain machine rows. The current official link target is
`https://cloud.google.com/products/compute/pricing/general-purpose#e2-machine-types`.
The page reader returns `document.body.innerText` only (`web_worker.py:668-670`)
and does not expose anchor `href` values. Consequently, after seeing “View prices”
in the excerpt, the model cannot discover that target from the page output unless
search or prior knowledge supplies it.

The current official general-purpose page does contain the rows when fetched
directly. An independent read-only `uv run python` probe on 2026-11-02 used
`urllib.request` plus BeautifulSoup `get_text(' ', strip=True)` (not Jarvis's
Selenium worker) and found `e2-standard-2`, `2`, `8 GiB`, and a default hourly
price row, plus `e2-medium`, `2`, `4 GiB`, and its default hourly price row. With
whitespace-normalized text, those rows occur around offsets 90,218 and 137,275;
the separate probe details and exact snippets are in
`official-page-probe.md`. They cannot appear in an 8,000-character prefix. This
confirms that the missing price was an extraction/path limitation, not proof that
Google publishes no price.

## Hard limits that made recovery impossible

- Live config: headed Chrome, 60-second operation timeout, 8,000 characters per
  page, 48,000 total evidence characters, moderate limits of three queries and
  five page attempts (`/home/samik/Projects/Jarvis/other/config/jarvis.toml`,
  `[web_search]`; values also recorded in the incident trace).
- Each page is charged before/after bounded extraction and a successful page is
  returned as a prefix. `web_tools.py:432-466` iterates the finite page allowance
  and slices returned text to `max_chars_per_page`; `web_tools.py:480-491` marks
  the result `truncated` with `stop_reason: page_text_limit`.
- The page reader itself slices again at `web_worker.py:668-700`. It records
  `source_chars`, `truncated`, and `extraction_method: body_prefix`, but has no
  section continuation or offset parameter.
- A fragment only checks whether an element with that ID/name exists; it does not
  scroll or extract that section. The documented behavior explicitly says that
  `section_found` is not proof that the section's content is in the returned
  prefix (`skills/web/SKILL.md:43-50`).
- Rendered mode would not solve this by itself: it requires a prior static read of
  the same canonical URL (`web_tools.py:385-398`), consumes another page attempt,
  waits only for visible text, and still returns the same bounded body prefix.
  It cannot click “View prices,” select a region, operate the Pricing Calculator,
  or scroll to a table (`web_worker.py:758-776`; `skills/web/SKILL.md:36-42`).
- The web skill tells the model to inspect each result, start with one or two
  useful pages, use static first, and preserve uncertainty when a needed price is
  not in evidence (`skills/web/SKILL.md:8-31, 43-50, 65-72`). The trace shows the
  model did not use the remaining path to the general-purpose page before the
  three-query and five-page ceilings were exhausted.

## Primary-source conclusion

The exact causal chain is:

`over-specific searches -> no official E2 table URL -> legacy/overview URLs ->
body-prefix excerpts omit machine tables -> page attempts exhausted -> no
recovery route`

The appropriate product fix is a research-path improvement: make the focused
official general-purpose URL discoverable/readable (or provide a safe section/
continuation mechanism), and reserve one page attempt for it after inspecting the
overview. Increasing the timeout or blindly retrying the same overview would not
address the main loss: the needed rows are on a different page and far beyond the
prefix. Any future price answer must still state region, billing model, and disk/
network exclusions; the reader cannot operate Google's interactive calculator.

## Source references

- Incident trace: `.scratch/pricing-research-diagnosis/web-trace.json` (tool
  ten call/result records: runtime-trace.jsonl.7 lines 390-418 and
  runtime-trace.jsonl lines 4-5).
- Live configuration: `/home/samik/Projects/Jarvis/other/config/jarvis.toml`
  `[web_search]` values obtained read-only over `jarvis-tailscale`.
- Local/deployed-equivalent implementation: `.scratch/pricing-research-diagnosis/live-source/web_tools.py`;
  `web_worker.py`; `skills/web/SKILL.md`.
- Google Cloud official overview and navigation:
  <https://cloud.google.com/products/compute/pricing>
- Google Cloud official general-purpose machine pricing table:
  <https://cloud.google.com/products/compute/pricing/general-purpose#e2-machine-types>


## Verification and limits

- Original run: October 31, 2026, 23:42-23:46 Asia/Amman; approval rejection
  followed November 1 at 00:03. The tool events use UTC (subtract three hours).
- Active release verified during this investigation: `f8f78112d5e1c98473be588709e1bd86c6d7f412`.
  The original terminal result also identifies this release. Relevant web modules
  and web skill match the local files after line-ending normalization.
- `uv run --no-sync python .scratch/pricing-research-diagnosis/replay.py`
  replays captured browser outputs and actual tool arguments through `WebTools`.
  It reproduced the exact remaining allowances and fails the E2-row-evidence
  assertion: neither requested machine row is present; the two overview excerpts
  are identical. This is a deterministic historical replay, not a fresh model run,
  an independent reproduction of Google's historical search ranking, or a fix.
- Successful reads returned `outcome: ok` together with truncation flags. Those
  flags were available to the model. There is no trace evidence of CAPTCHA,
  page timeout, rendered attempt, or global evidence exhaustion in the original
  run. The hard exhausted resources were query and page counts.
- A current public page probe establishes present-day accessibility and structure,
  not the exact complete October 31 DOM, which was not archived in these records.
- No runtime implementation, configuration, deployment, or access controls changed.

## Separate note-request incident (November 2)

The request to create a planning-only note explicitly said not to rerun research,
then quoted a future demo prompt. Jarvis nevertheless loaded web tools, proposed
three host inspection commands, and performed fresh pricing research. Operator
replies approved the three commands. Its proposal included the improvement plan
but also unsolicited research and an inaccurate scope statement that research
was not rerun. This is separate evidence of instruction-following failure; it
must not be mixed with the October 31 pricing incident. Replacement with a
concise planning-only note was authorized. The overexpanded proposal was rejected after browser recovery. A corrected
planning-only proposal was approved once, saved and synced, then read back by
Jarvis at 20:07 Asia/Amman. The readback returned freshness `synced` at vault
revision `4edfe91ad2e381c94147a111b32accfdd1becd82`; its 1,450-character body
contains only the checklist. `/status` subsequently reported no active request
and no pending action. See `note-verification.json`, `planning-note.md`, and
`note-saved.jpg`.
