# Web-search simplification deployment and acceptance

Date: 2026-10-29 (Asia/Amman)
Status: complete

## Candidate and deployment

- Deployed candidate: `f8f78112d5e1c98473be588709e1bd86c6d7f412`.
- GitHub main was verified at that exact SHA before staging. At resume, GitHub
  main was `b71ddbb1316df1539a0e6c86fbc7e0bc520f2e1b`, a descendant containing
  checkpoint documentation; the candidate's ancestry was verified locally.
- Active directory: `/home/samik/Projects/Jarvis/code/releases/f8f78112d5e1c98473be588709e1bd86c6d7f412`.
- Rollback release retained: `9520982fd6fc6f780dfe975b4a6bbe61789b7ff7`.
- Fresh comparison against production preserved all unrelated runtime behavior;
  only `web_tools.py`, `web_evidence.py`, `web_worker.py`, and the web skill differ.
- Hash-locked dependency installation, non-editable package, service-account
  `--check`, and byte comparisons of all 28 packaged files passed.
- Installed tool contract: maximum five URLs, static/rendered modes, login-widget
  public-text guidance, pricing guidance, and immediate terminal exposure passed.
- Live prompt matched candidate after line-ending normalization. The older
  readiness helper's obsolete-example comparison is superseded by this check.
- Activation succeeded after the operator restored SSH. Pre/post OpenWA readiness
  was HTTP 200, exact named session ready; service active/running, NRestarts=0.
  Private listener verified at 172.20.0.1:9011.
- Configuration and SYSTEM.md were unchanged. SHA256 values:
  config `b6f15064ead9fbff6f543c616ad9c047c0be097df550910450a424515e5a76be`;
  prompt `33ca22c6f7c7b5b162bd29014139b10ee5b192fec3395136d6bac1672306ba0a`.

## Validation

- Ubuntu full runtime suite: **581 passed, no skips, 207.90 seconds**, including
  real headed/headless Chrome, login widgets, challenge stops, separate anonymous
  profiles, public-network restrictions, bounded rendering and cancellation cleanup.
- All 59 tested runtime/test files matched the verified release source.
- Windows full runtime suite: **573 passed, 8 platform skips, 345.20 seconds**.
- Expanded web tests: **118 passed, 1 POSIX skip, 28.17 seconds**.
- Ruff check, format check (59 files), compileall and diff checks passed.
- The 800-character legacy fixture cannot hold 811 characters of full metadata.
  Its regression now expects explicit output-limit/no evidence charge at 800,
  and intact source identities, extraction metadata and clipped text at 1000.
- Browser fixture stabilization: static cold-start headroom increased to 30 seconds
  after the unchanged production worker measured 13.93 seconds. Cleanup assertions
  allow bounded descendant exit after SIGKILL. Production limits were unchanged.

## Visible WhatsApp acceptance

Authorized read-only requests were sent through the in-app Jarvis conversation.
Trace start: 2026-10-29T09:41:34.101659Z.

1. Search and citation: Google returned five results; the model read IANA's
   `https://www.iana.org/help/example-domains` statically (1256 characters,
   untruncated). Visible reply at 12:42 correctly explained documentation/example
   use, no registration/transfer, and no production-service guarantee, citing IANA.
2. Static/rendered comparison: `https://quotes.toscrape.com/js/` returned 78
   static characters without quotes. A separate rendered read matched expected
   `Albert Einstein` and returned 1499 untruncated characters. Both attempts
   consumed the quick two-page allowance. The visible 12:43 reply supplied the
   requested Einstein quote with a source citation and accurately described the
   static/rendered difference.
3. Anonymous public text on login-widget page: the static read of
   `https://quotes.toscrape.com/login` succeeded with 89 untruncated characters.
   The visible 12:44 reply reported Quotes to Scrape, Username/Password labels,
   GoodReads.com and Zyte footer attribution with a citation. No credentials,
   form submissions, authenticated content, terminal or unrelated skills were used.
4. Final visible `/status` at 12:45: **Active request: none; pending action: no**.
   Service-account health found zero Chrome/ChromeDriver/Xvfb/worker processes and
   zero temporary profiles. Service active/running, NRestarts=0; OpenWA HTTP 200
   and exact named session ready. Live config and prompt hashes remain unchanged.

Logs retained locally (ignored): ubuntu-suite.log, windows-suite.log, build.log,
activation.log. Sanitized [tool trace](whatsapp-trace.json) and the cropped
[visible reply/status screenshot](whatsapp-acceptance.jpg) accompany this record.
Protected unrelated local modifications remain untouched and unstaged.
