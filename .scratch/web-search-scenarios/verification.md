# Web search scenario testing — 2026-10-17–18

Status: complete. Fix deployed and verified through the in-app WhatsApp chat.

## Fix

`src/jarvis_personal_runtime/web_search.py` recognizes Arabic Google empty-result messages, including the observed bottom status container. An exact-phrase empty notice no longer hides source headings in an unknown result layout: that case remains `extraction_failed` instead of falsely claiming no results.

Six regression cases cover Arabic and English top/bottom messages, known and unknown unquoted-result layouts, and empty-result words inside source content. The regression was reproduced before fixing (4 failed, 24 passed), then all 28 parser tests passed. Captured live HTML now returns `no_results`.

## Validation

- Full Windows checkout: 514 passed, 6 skipped.
- Earlier isolated Ubuntu checkout: 518 passed.
- Final candidate based on the actual deployed revision: **520 passed in 145.47 seconds**, including real Chrome/ChromeDriver tests. See `deployed-tests.log`.
- Ruff lint, formatting, and whitespace checks passed for the two changed files.
- Wheel built and installed non-editably with hash-locked requirements; service-account CLI `--check` passed before activation.

## Deployment

- Prior/rollback: `e69864c083a706ec7f4e8d5bfc8cb0cda46ae17e`.
- Active: `5dfac1eebb68d32f7a0f60ea1b23616bc1a93662`.
- Candidate built from the prior live revision; only parser and regression tests changed (70 insertions, 13 deletions).
- Atomic `code/current` activation succeeded with rollback protection.
- Installed parser hash equals candidate source hash: `0b06ec62df64ee7145ecfa28d2012dac390ecfbcacf6309c3cae594f981deb87`.
- Existing deployed vault code preserved byte-for-byte: SHA256 `8baa9ab2894b58b90fea914b241be80b083c9f2acf2277f5f69d2b6aa5407ca1`.
- Configuration unchanged: SHA256 `3c638aae8d216b145b8e41f73502137500c3ae65901270ee826237cf1ce07d4f`.
- SYSTEM.md content unchanged: SHA256 `eb5ab2cd13457ba4a3b0db2a702c2bb9c7c1ce967e1a84e31335c1c684373247`.
- Final service active/running, NRestarts=0; OpenWA health HTTP 200 and exact named session ready; private listener `172.20.0.1`.
- Installed web skill exposes `search_web` and `read_web_pages`; live prompt guidance verified.
- Final cleanup: zero service-owned Chrome, ChromeDriver, Xvfb or web worker processes; zero temporary web profiles.
- Deployed commit retained locally on `codex/web-search-empty-results-deployed` and in `deployed-fix.bundle`. The original checkout and unrelated dirty vault files were preserved; its two search files exactly match the deployed commit. No push performed.

## Live installed-tool baseline (before fix)

| Scenario | Observation |
| --- | --- |
| Official IANA quick query | `ok`, four sources |
| Open opaque Google result link | `ok`, resolved to IANA |
| Second quick query | `limit_reached`, no navigation |
| Python asyncio technical query | `ok`, five sources |
| Arabic currency query | `ok`, five sources |
| Current Python release query | `ok`, five sources |
| Nonexistent quoted phrase | `extraction_failed`; regression fixed |
| Batch IANA, Python docs, PDF | Two HTML reads `ok`, PDF `unsupported` |
| Loopback URL | `blocked` before navigation |
| Profile cleanup | Passed after every request |

Full baseline: `live-baseline.json`. No paid search service or personal browser state used.

## WhatsApp acceptance before deployment

Observed on October 17 (Asia/Amman); sanitized trace in `previous-chat-trace.json`:

- 15:39 latest Python query: Google blocked the query; Jarvis disclosed failure without inventing a version or citation. No challenge interaction or retry.
- 19:55 direct IANA read: accurate cited answer.
- 20:50 evidence-reuse follow-up: correct one-sentence answer and same citation. Recovered trace confirms no tool call between this request and the next request.
- 20:52 Arabic comparison: two pages read with `moderate`; accurate IANA/RFC 2606 distinction and both citations.
- 20:53 mixed HTML/PDF: `partial`, IANA `ok`, PDF `unsupported`; useful supported answer and explicit PDF limitation.
- Final status idle, no pending action.

## WhatsApp acceptance after deployment

October 18, Asia/Amman; trace timestamps below are UTC. Sanitized metadata: `postdeploy-trace.json`. Visible evidence: `whatsapp-postdeploy.png` and `whatsapp-postdeploy-status.png`.

1. At 15:15:30Z, Jarvis called one quick search for `"jarvis-no-match-70b482f9-20261017-zqxv"`. At 15:15:55Z it returned `no_results`, zero results, and zero remaining queries. Visible answer: “No useful search results were found. No results were returned for the exact quoted phrase.” No broadening or retry.
2. At 15:16:31Z, a fresh `site:iana.org example domains` search returned `ok` and four sources. Jarvis opened two returned opaque links, which resolved to `https://www.iana.org/domains/reserved` and `https://www.iana.org/news/2024/example-domain-http-methods`; both returned page evidence `ok`. The visible reply correctly said example.org cannot be registered/transferred and its HTTP service should not be relied on in production, citing both pages. Usage remained within one query/two pages, with 26,721 evidence characters remaining.
3. Final `/status` checked after replies; no active request or pending action. Service/readiness/profile cleanup checked after acceptance.

Google blocked an earlier baseline request, so successful acceptance does not guarantee future Google availability. The blocked-path behavior remains explicit and does not bypass challenges.

Final visible `/status` at 18:17 Asia/Amman: model gpt-6-luna (medium); active request none; pending action no; saved permissions 12; context approximately 11,827 / 100,000 tokens (local estimate).
