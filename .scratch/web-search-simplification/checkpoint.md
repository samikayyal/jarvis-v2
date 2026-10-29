# Recovery checkpoint — 2026-10-29

Status: in-progress

## Latest checkpoint

- Exact candidate: `f8f78112d5e1c98473be588709e1bd86c6d7f412`, committed and
  pushed to GitHub main; exact remote hash verified before deployment attempt.
- Ubuntu full suite PASSED: **581 passed, no skips, 207.90 seconds**; Ruff check,
  formatting (59 files) and compileall passed. Log on Ubuntu at
  `/home/samik/Projects/Jarvis/code/web-simplification-test-ImAQZT/suite-acceptance.log`.
- Compared all 59 tested runtime/test files against the candidate release: match.
- Hash-locked non-editable package built in `code/releases/<candidate>`;
  service-account --check and all 28 installed files passed. Web schema maxItems=5,
  both render modes, login-widget instructions, immediate terminal and pricing
  guidance verified. Live prompt matches candidate after CRLF/LF normalization.
- Real-browser test stabilization only: 30-second cold static/startup allowance
  (production worker measured 13.93 seconds), and bounded polling after SIGKILL
  for descendant exit. Render wait bound unchanged; all isolation/cleanup tests pass.
- WhatsApp was reattached by the user. Visible predeployment /status at 04:13
  Asia/Amman: active request none, pending action no. No acceptance scenarios sent.
- **Activation not run:** SSH to 100.106.206.88:22 timed out before connection;
  escalated read-only retry and TCP probe also failed. Last verified active source
  remains `9520982fd6fc6f780dfe975b4a6bbe61789b7ff7`. Recheck before resuming.
- Once SSH returns: read current and activation log first. Then run the committed
  candidate's activate.py as root through uv; follow with visible WhatsApp tests,
  final idle/cleanup, evidence commit/push, and complete status. Source work is done.

## Earlier recovery history

- Resumed independently in recovery chat `01a18521-54a9-7890-bf83-73c97b16ecd4`.
- Seven saved implementation/test changes reviewed. Fixed final fitting to retain
  complete small excerpts and their metadata; added regression. Updated detailed
  tool documentation for five URLs, anonymous public text, detection and fitting.
- Focused Windows tests: 77 passed, 7 skipped (POSIX/real browser), 23.03 seconds.
- Ruff check, format check (59 files), compileall and diff checks passed.
- First full Windows run: 570 passed, 8 skipped, one legacy output-cap assertion
  failed. The fixture's complete two-source metadata is 811 characters at an
  800-character cap; old fitting discarded metadata. Updated test requires an
  explicit limit/no evidence charge at 800 and preserved metadata/excerpts at
  1000. Expanded focused adapter/quality run: 87 passed in 10.68 seconds.
- Full Windows rerun passed: 573 passed, 8 platform skips, 345.20 seconds.
  Expanded web tests: 118 passed, 1 POSIX skip, 28.17 seconds.
- Ubuntu staged source: /home/samik/Projects/Jarvis/code/web-simplification-test-ImAQZT.
  First suite: 578 passed, two failures (the cap assertion above and a headed
  initial-navigation timeout). Isolated real-browser rerun: four cleanup tests
  passed; both rendered scenarios hit the initial 15-second static navigation
  timeout. Current suite log: suite-final.log. No candidate deployed.
  Startup diagnostic: active worker 13.93 seconds, candidate 9.21 seconds; both
  succeeded with identical evidence. Raised only static/cleanup fixture startup
  allowance to 30 seconds; rendered wait bound is unchanged. Fresh serial full
  suite running in suite-verified.log (no overlapping browser runs).
- Fresh Ubuntu active release: `9520982fd6fc6f780dfe975b4a6bbe61789b7ff7`;
  active/running, NRestarts=0, OpenWA HTTP 200, exact named session ready.
- Fresh normalized source comparison: only the four intended runtime files
  differ (web_evidence.py, web_tools.py, web_worker.py, skills/web/SKILL.md).
  No missing/new runtime files; deployment, pyproject and lock files match.
  Generated egg-info SOURCES.txt differs and is excluded from source comparison.
- Config SHA256: b6f15064ead9fbff6f543c616ad9c047c0be097df550910450a424515e5a76be.
- Live prompt SHA256: 33ca22c6f7c7b5b162bd29014139b10ee5b192fec3395136d6bac1672306ba0a.
  The historical readiness helper compares an obsolete example and reports false;
  candidate prompt must be compared directly to the live prompt.
- No service-account browser processes or temporary profiles before acceptance.
- New in-app WhatsApp tab opened, connected, Jarvis selected; no messages sent yet.
- Protected unrelated paths remain untouched and unstaged.

Remaining: full suite, Ubuntu real Chrome suite, exact GitHub candidate verification,
hash-locked installed package and service-account checks, activation with rollback,
visible WhatsApp citations/static-rendered/login-widget acceptance, final idle and
cleanup checks, committed/pushed evidence, then complete the ticket.
