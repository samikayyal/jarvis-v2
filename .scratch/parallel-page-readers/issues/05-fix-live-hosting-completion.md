# 05: Restore completion of the live hosting-and-note workflow

**Status:** needs-triage

**What to investigate:** Ticket 04's deployed candidate passed its Ubuntu suite
but the original visible WhatsApp request timed out before saving a note. Resolve
the demonstrated live failures before repeating ticket 04 acceptance. This is
follow-up work to scope and triage, not part of the original four-ticket sign-off.

2026-11-08: The operator requested continuing through the demonstrated blockers
and completing ticket 04. This investigation is now included in that continuation;
the agreed regression seams are the existing browser, reader-tool and main
request-flow public interfaces. Vault-edit redesign remains excluded.

## Evidence

See [deployment and acceptance](../deployment-verification-04.md) and
[measured trace summary](../verification/hosting-metrics-04.json).

- Candidate `551ed85`: 600.11 seconds to timeout, 603.41 through delivery;
  49 main calls, 8 reader calls, no `edit_vault` call. The baseline completed
  in 458.128 seconds (460.284 through delivery), with 22 main and 27 reader calls.
- Three initial concurrent GCP readers reached their 120-second assignment
  limit before a first model call. Later attempts read the sources. Identify
  time spent in browser launch, navigation/redirects, settling, extraction and
  context preparation; do not infer a network or host-capacity cause from the
  total alone. Use representative real GCP pages alongside fixtures.
- Main orchestration made 9 reader starts, 9 waits, 8 status collections,
  8 terminal calls, 3 searches and 5 releases. Completed main-model calls used
  303.04 seconds; the last unfinished call waited another 27.28 seconds.
  Searches used 91.28 seconds; main waits used 153.78 seconds. Reader wall time
  overlaps main work. Do not add the nested reader-model time to it.
- Default terminal work began in `legacy-workspace`, producing long obsolete
  V1 deployment inventories. A recovery note needed operator correction of
  current native-service vs historical-bundle claims and sample chronology.
  Establish the intended source/context route before changing that setting.
- One reader follow-up reported that its browser was closed while its returned
  execution state said open; inspect stale loss-of-state notice interpretation.
- The approved corrected recovery note was committed and pushed, but the next
  main continuation reached the working-session context limit. Preserve the
  distinction between an executed/synced write and unsuccessful final synthesis.
- A live journal contained an unretrieved Playwright `TargetClosedError` future
  during timeout cleanup; verify lifecycle exception handling as well as absence
  of surviving processes. One progress send had uncertain/error acknowledgement.

## Follow-up acceptance

- Bound research by the hosting decision and preserve time/context for synthesis
  and the requested save. Report specific unknown prices and use supporting
  primary evidence; do not revive mandatory scanning or quote-gating policy.
- Verify source loading and main/reader lifecycle with focused regressions at
  agreed seams. Keep vault-edit redesign separate: the failed benchmark never
  reached a vault edit, so this run does not establish it as the bottleneck.
- Repeat the exact original visible WhatsApp request with a fresh session,
  saved/synced note, concise final recommendation, claim-level inspection and
  measured completion/delivery. Record under-two-minute attainment separately
  from functional success; retain GitHub exact-candidate deployment gates.

## 2026-11-08 outcome and remaining scope

The operator-authorized continuation restored completion on `9c2f7d9`:
the exact original request completed, saved and synced the note, and visibly
delivered its concise recommendation. Claim-level inspection and cancellation
cleanup passed. Ticket 04 is complete; see [corrective evidence](../continuation-verification-04.md).
This resolves the original no-save timeout, but does not resolve every item here.

Keep this follow-up open for these demonstrated residuals:

- A timeout/close race logged `AttributeError exception in shielded future`:
  `PlaywrightBrowser.observe()` read `self._page.url` after concurrent close
  cleared the page. The action was drained and no browser/driver survived,
  but exception handling is not clean. Add a deterministic cancellation-during-
  observation regression at the already approved public browser seam, then fix
  the race without weakening authority or resource cleanup.
- Completion took 507.389 seconds (28 main, 8 reader calls), exceeding the old
  completed 458.128-second baseline and the 120-second target. Main model time
  was 361.649 seconds; reader waits 59.379 seconds and searches 34.057 seconds
  were also material. The overview/calculator assignments' terminal intervals
  were 233.694/154.671 seconds. Terminal events occur after cleanup, so isolate
  research, deadline handling and teardown before attributing the overrun.
- One visible progress update and the final reply had uncertain transport
  acknowledgements. Read-only exact-body gateway history and the visible chat
  confirmed the final was delivered. Preserve duplicate-send protection while
  investigating this uncertainty; do not automatically resend a possibly sent
  message.

The batched observation fix, corrected reopened state and runtime working
directory are deployed and verified. Vault-edit redesign remains excluded:
the successful run's single edit took 11.909 seconds and synced normally.
