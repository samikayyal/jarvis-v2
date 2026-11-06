# Full-access deployment and WhatsApp acceptance

Date: 2026-11-06 (Asia/Amman)
Status: deployed; core full-access WhatsApp checks passed; remaining UI checks incomplete.

## Source and activation

- Deployed `39765480870245a87abc6ee5d9d689af5d6f8aee` from `main`, including
  full access and the page-reader coverage changes.
- Verified the exact SHA at GitHub's `refs/heads/main` before staging and again
  before activation. Source was already committed and pushed.
- `git archive` SHA256, matching after Ubuntu transfer:
  `d13463cc1f994e9f7f85c3cd60966d66c1d23456a3109a00f5f67f9fa0f360c1`.
- Installed hash-locked dependencies and the non-editable package in
  `/home/samik/Projects/Jarvis/code/releases/39765480870245a87abc6ee5d9d689af5d6f8aee`.
- All 30 installed runtime/source skill files match the candidate archive.
- Switched `current` after inactive validation, retaining prior release
  `df91f127224c7c5789d6aabbeee15f656139849e` for rollback.
- Installed the candidate's committed `SYSTEM.md.example`. The prior live prompt
  matched the old example and was backed up as
  `other/runtime/backups/SYSTEM.pre-full-access-3976548.md`.
  New prompt SHA256: `9beb38f5530a9bf10fe3159576ee016957ca7227622a2530fd22ca172d3d3db1`.
- Activation helper retains rollback of both prompt and release on startup failure.

## Candidate and final host verification

- Exact-candidate Ubuntu suite: **640 passed, 1 skipped**, 288.50 seconds.
  Real Chromium/ChromeDriver fixtures enabled. The separately opt-in local-browser
  test remains skipped.
- Ruff passed; all 65 checked Python files formatted.
- Candidate CLI `--check` passed as the service account.
- Reader configuration remains `gpt-6-luna`, 24 model turns, 18 actions,
  120 seconds, 128000 context tokens.
- Runtime enabled and active/running, with `NRestarts=0` after activation and
  after acceptance. No Chrome or browser worker processes or temporary profiles
  remained. The persistent Node companion was present.
- OpenWA readiness HTTP 200; exact configured session ready and identity matched.
- Final installed-source, configuration, and prompt verification passed.

## Visible WhatsApp acceptance

Messages were sent in the Jarvis conversation using WhatsApp Web in the in-app
browser, approximately 16:49–16:56 Asia/Amman. No API or webhook substitute was
used. Replies were read in the UI and correlated with runtime events and the
test file. Final cancellation is confirmed by the runtime trace; browser access
stopped before its acknowledgement could be reread in the UI.

| Scenario | Result |
| --- | --- |
| Fresh session after deployment restart | `/status` showed full access off, no active request or pending action, 15 saved permissions. |
| Normal-mode write | A unique `printf` append to `/tmp/jarvis-full-access-20261106-1650.txt` paused for approval. No execution occurred before enable. |
| Enable with pending action | `/full-access` resumed the exact pending write once; visible acknowledgement on and exit-code-0 completion. |
| Repeated enable | A second `/full-access` acknowledged on without replaying the write. |
| Automatic subsequent actions | A second append and a read executed without approval prompts. Trace contains `full_access_approval` for each; visible reply showed exactly `pending-resume` and `automatic`. |
| Disable | `/full-access off` acknowledged off. The next `must-not-run` append paused for approval. |
| Malformed control while pending | `/full-access nope` returned invalid-use guidance and did not execute the pending action. |
| Cancel pending action | `/cancel` produced a traced cancellation with full access off. Test file still contains only the two expected lines. |
| Saved permissions | Config hash unchanged: `9d76db623deef741509923396b36e46c67d5b4ab94ffa8a07a105ebb94f90c59`; no saved permission added. |

The harmless two-line test file is retained at the path above as evidence.

## Remaining acceptance and known limitations

Browser/computer-use session recovery failed with
`failed to write kernel assets: The system cannot find the path specified. (os error 3)`.
Remaining live checks were not executed: cancellation while enabled, `/new`
revocation, restart from an enabled session, and a post-deployment page-reader
smoke test. Automated lifecycle tests cover cancellation, new session, expiry,
context reset, and reconstruction, but do not replace those UI checks.
No live Gmail, Calendar, or vault mutation was attempted during this acceptance.

The existing page-reader refresh-interpretation and quote-repair failures remain
tracked in [the reader feedback issue](../page-reader-coverage/issues/02-live-reader-feedback.md).
This deployment includes the current page-reader code; it does not fix those
known issues or establish an all-features clean acceptance pass.

Resume in the same Jarvis WhatsApp conversation. Confirm idle/off with `/status`,
then test the remaining lifecycle cases and reader smoke flow. Leave full access
off after testing. The staging, verification, activation, and filtered trace
helpers in this directory record the procedure used here.
