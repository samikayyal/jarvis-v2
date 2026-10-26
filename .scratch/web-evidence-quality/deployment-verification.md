# Web evidence quality: production deployment and WhatsApp acceptance

Deployment date: 2026-10-24. Scope: points 2–6 from [spec.md](spec.md), followed by
real authorized read-only WhatsApp tests. Section-aware extraction remains excluded.

## Release and preservation

- Active release: `9520982fd6fc6f780dfe975b4a6bbe61789b7ff7`.
- Previous/rollback release: `ff501df0b58c9e38eaa631f17b1e4b9be62a8bdf`.
- Release directory: `/home/samik/Projects/Jarvis/code/releases/9520982fd6fc6f780dfe975b4a6bbe61789b7ff7`.
- Config/release rollback record: `/home/samik/Projects/Jarvis/other/backups/web-quality-20261024T164206Z`.
- Deployment source: `/home/samik/Projects/Jarvis/code/web-quality-candidate-bcQpYy`,
  branch `codex/web-evidence-deployment`; recoverable local `deployment-source.bundle`.
- Production had newer assistant/terminal behavior than the local checkout. The
  candidate starts from an explicit snapshot of the deployed release, with only
  the scoped web patch applied. A normalized comparison verified exactly six
  runtime changes: `config.py`, `web_browser.py`, `web_evidence.py`, `web_tools.py`,
  `web_worker.py`, and `skills/web/SKILL.md`. Existing pricing guidance was retained.
- The active SYSTEM.md is unchanged, SHA256
  `33ca22c6f7c7b5b162bd29014139b10ee5b192fec3395136d6bac1672306ba0a`.
- Local preexisting vault/search modifications and unrelated work remain intact.
  Deployment commits were made in the isolated Ubuntu candidate, not the user's
  dirty checkout. The candidate's only post-build diff is generated egg-info.

## Configuration and package evidence

- Enabled; headed anonymous Chrome; 60-second operation timeout.
- Quick: 1 query / 2 page attempts. Moderate: 3 queries / 5 page attempts.
- Five results per query; 8,000 characters per page; **48,000 total evidence characters**.
- Only the live total-evidence setting changed from 32,000. Parsed TOML equality
  confirmed all other settings were preserved.
- Hash-locked runtime dependency installation, non-editable wheel, service-account
  `--check`, and prepared-tool contract checks passed before/after activation.
- Installed files, wheel contents and release source matched for all 28 packaged
  files. Wheel SHA256:
  `1d8e163decb929d810169d12576357115fa0b0cf64a9382f7bec9f828ffbd3bd`.
- Contract check verified static/rendered mode and wait text, preserved pricing
  instructions, immediate terminal exposure, live prompt skill-loading guidance,
  and the updated evidence allowance.

## Exact deployed-source tests

**570 passed in 216.38 seconds**, no skips, including real headed/headless browser
coverage. Ruff check, Ruff format check (59 files), compileall and diff checks passed.
This supersedes the earlier 559-test local-base result for deployment purposes.
See [deployed-base-suite.log](deployed-base-suite.log) and [activation.log](activation.log).

## WhatsApp acceptance

Tests were sent through the existing Jarvis WhatsApp chat. No terminal/vault writes
were requested. Sanitized tool evidence is in [whatsapp-trace.json](whatsapp-trace.json).

1. **Static-to-rendered JavaScript page:** quotes.toscrape.com/js returned 78 static
   characters without quotes; the rendered follow-up waited for `Albert Einstein`,
   matched readiness, and returned 1,499 characters. Both attempts consumed the
   quick page budget. The visible reply quoted the first quote correctly, named
   its author, explained the difference and cited the page.
2. **Five-page evidence and truncation:** Python documentation was read in batches
   of two then three. All five reads succeeded, returning 3,720 + four 8,000-character
   excerpts = **35,720 characters**. Remaining evidence was 12,280 and page attempts
   zero. The visible reply cited a supported fact from every page, distinguished
   the four truncated excerpts from the full asyncio page, and reported the page limit.
3. **Search and source verification:** one Google `site:iana.org example domains`
   query returned four organic results. The following IANA page read succeeded
   with 1,256 characters. The visible reply separated search evidence from page
   verification and cited both. Google did not challenge this test; this does not
   establish permanent search reliability.

4. **Original cloud-pricing case:** both Compute Engine and Cloud Run static reads
   succeeded with 8,000-character truncated excerpts. Compute Engine redirected to
   `/products/compute/pricing`; the requested anchor was not detected. The rendered
   follow-up waiting for `e2-standard-2` reached the 60-second operation timeout,
   returning no page text and consuming another page attempt. The reply explicitly
   reported one timeout and two usable excerpts, left E2 price/region unverified,
   distinguished Cloud Run pricing from Compute Engine pricing, and correctly
   stated that a missing anchor does not establish that the section is absent.
   This validates failure reporting, not successful extraction of E2 prices.

## Final health and practical limitation

On 2026-10-25, the same release remained active/running with `NRestarts=0`. The
gateway returned HTTP 200; the exact configured named session was ready and its
identity matched. The 48,000-character allowance and unchanged prompt hash were
reconfirmed. There were zero service-owned Chrome/ChromeDriver/Xvfb/web-worker
processes and zero request profile directories. See [final-health.log](final-health.log)
and [final-readiness.log](final-readiness.log).

A fresh WhatsApp `/status` at 15:05 Asia/Amman on 2026-10-25 visibly reported
`Active request: none; pending action: no`, model `gpt-6-luna (medium)`, and 12
saved permissions. See `whatsapp-final-status.png`. The completed scenarios and
final idle check satisfy the deployment acceptance scope; the site-specific
pricing timeout remains explicitly documented rather than represented as a pass.

The pricing timeout is a remaining website-specific access limitation, not evidence
that rendering fails universally: the JavaScript quotes test succeeded end to end.
Section-aware extraction is still excluded; no claim is made that this release
solves all pricing pages or permanently eliminates Google challenges.
