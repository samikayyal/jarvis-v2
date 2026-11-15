# Ticket 02 deployment and measurement

Deployment and benchmark: 2026-11-14 (Asia/Amman).
Final verification: 2026-11-15 (Asia/Amman).
Status: deployed and functionally accepted; two-minute latency target missed.

## Source and preflight

Runtime candidate: `fe70273c5551b2214443d1812365bfa160bc86aa` on `main`.
`git push origin main` succeeded; `git ls-remote origin refs/heads/main`
returned that exact SHA before the archive was created or transferred.
Archive SHA-256: `092eb31d3d8d754d2c91adaa26bcaa21e28cb9928cb4afe7e72d9f9c241d997e`.
The Ubuntu checksum check passed before extraction into the commit-named release.

The previous active release and rollback target is
`/home/samik/Projects/Jarvis/code/releases/9c2f7d90b867f63cdd536ac0970916ed8143e796`.
Its own interpreter verified all archived source files and 31 installed package
files against `code/readers-continuation.tar.gz`. Service active/running,
`NRestarts=0`; OpenWA container healthy, readiness HTTP 200, exact configured
named session `ready`. Visible WhatsApp `/status` at 17:12 local confirmed
idle, no pending action, full access off, 15 saved permissions, empty context.

Configuration SHA-256:
`d0efea96c32474876fa58449e50ea12306e836fd3ffb0bcadf5113366c87d9ec`.
Prompt SHA-256:
`9beb38f5530a9bf10fe3159576ee016957ca7227622a2530fd22ca172d3d3db1`.
Neither needs modification for the candidate; the batch maximum defaults to five.
The activation helper checks these hashes and verifies candidate and rollback
with each release's own interpreter and archive. Failed activation automatically
restores and checks the previous release. Existing browser stacks are retained.

## Inactive verification

Hash-locked runtime dependencies and a non-editable package were installed into
the new release. Service-account CLI `--check` and byte-for-byte installed-source
verification passed. Tests run from a separate archive extraction with the locked
development environment, existing Playwright cache and explicit Chrome/driver.

- Ruff lint, format (68 files) and compileall passed.
- Typecheck reports 46 existing diagnostics, matching ticket 01's candidate
  count. This is not a clean typecheck result.
- Initial focused parallel-tools run: 20 passed, one failed in 61.18 seconds.
  `/cancel` reader fixture timed out at `AssignmentModel.arrived()` before the
  scripted model was reached (20-second readiness wait). No cancellation
  assertion failed. This resembles, but does not prove the cause of, the earlier
  fixture readiness failures recorded in ticket 01.
- Unchanged focused rerun: **21 passed in 30.95 seconds**. No source, timeout,
  environment, or browser setting was changed between the runs.
- Full Ubuntu suite: **584 passed, 1 skipped in 523.74 seconds**, exit 0. The initial staging exit remains 1; activation requires
  the separately recorded focused rerun and full-suite exit codes to be zero.

## Live evidence method and baseline

Controlled requests used existing saved permission for `free -h` to run
bounded, read-only samples concurrently with exact, one-time-approved sleep/print
commands. They verified approval delivery before sibling completion, the next
approval while an approved action is running, retained results, execution counts,
and `/cancel` plus `/new` cleanup with a visible proposal. No persistent permission
was added for these fixtures.

The original benchmark prompt is:

> Should I keep hosting you on my Ubuntu laptop or move you to GCP? Save the full result in an Obsidian note. Keep your final WhatsApp summary short.

Starting vault HEAD is `9005e0e1c6701aa1499baab0cf92a12742abcabb`, clean.
`Technical/Jarvis hosting comparison.md` is unchanged since the previous accepted
run, SHA-256 `f5eadd168a0a223888bf92149d1221ac52aea2a0bc08ed94be96d5a7655b1c98`.
Its dated history was preserved, with the former current heading renamed
"Previous assessment". The comparison baseline is 507.389 seconds, 28 main calls
and 8 reader calls. Both hosting runs used session-only full access, so neither
included individual approval delays. Admission-to-result time is reported intact.

`measure-02.py` exports bounded sanitized trace metrics. Terminal overlap uses
actual executor events, not delayed batch results. Reader terminal intervals
include cleanup. Nested/overlapping spans are not summed into elapsed time.
Gateway acknowledgements and timestamps do not establish recipient receipt time.
Raw trace bodies, message identifiers and note contents remain private.

Controlled acceptance, benchmark and final cleanup results follow below.

## Activation and procedure review

Activation completed with exit code 0 on 2026-11-14. The active symlink resolves
exactly to `releases/fe70273c5551b2214443d1812365bfa160bc86aa`. Post-start
installed-source verification passed, the configured session was ready with
HTTP 200 gateway readiness, and the service was enabled/active/running with
`NRestarts=0`. Configuration and prompt were unchanged. The prior release remains
available as the verified rollback target.

Operational commits `b8b479c` and `b40ddcb` were pushed; GitHub main was checked
as `b40ddcbc9f5591887cd241313fe4160d2cbe5a49`, containing the immutable runtime
candidate, before activation. The existing systemd world-writable-unit warning
and unrelated obsolete CPUAccounting warnings were recorded, not changed.

Independent Standards and Spec procedure reviews each found one issue in the
measurement helper: raw control/reason text could escape sanitization, and
identical concurrent terminal commands could receive guessed FIFO attribution.
The helper now allowlists exact controls, omits reader exception reasons, and
marks repeated overlapping command attribution ambiguous. Both reviewers
confirmed their findings resolved. No runtime source changed after the full suite.

The first post-activation UI attempt stopped because Computer Use could not
confidently determine Chrome's URL. The operator requested the in-app browser;
WhatsApp opened there but redirected to a logged-out page. At that checkpoint,
post-activation ordinary-request acceptance and the benchmark were still pending.

## Controlled live acceptance

The operator linked the in-app WhatsApp browser after Chrome control failed.
Visible `/status` at 17:47 local confirmed idle, no pending action, full access off
and 15 saved permissions. The runtime's send acknowledgement was `unavailable`,
`may_have_sent=true`; the actual reply was visible. It was not resent.

### Overlap and two approvals

Request P02-A asked for one batch containing four exact Ubuntu terminal calls:

1. `free -h -s 1 -c 90`
2. `free -h -s 2 -c 45`
3. `sleep 75; printf 'P02_A_APPROVED_ONCE\n'`
4. `printf 'P02_A_SECOND_ONCE\n'`

The main response requested all four together. The first two used the existing
`free -h` saved permission; the other two each received one-time choice `1`.
No saved permission was added. Execution starts were 14:48:13.633142Z and
14:48:13.640584Z; they overlapped for **88.066 seconds**. The first approval was
produced at 14:48:13.652754Z and acknowledged sent at 14:48:16.099376Z, while
both calls were running; it was visibly inspected before approval.

The approved sleep/print started at 14:48:29.659527Z. The second approval was
produced at 14:48:29.664753Z and acknowledged sent at 14:48:31.841103Z, while
that operation and both original siblings were running. Process inspection
independently confirmed those three processes. The second action started at
14:48:47.625471Z, finished once, and its result was retained until all siblings
finished. All four exited zero, with no timeout or uncertain remote effect.
The four call-ID results returned in original order at 14:49:44.680111Z onward.
The final was completed at 14:49:50.190749Z, acknowledged sent at
14:49:52.641579Z, and visibly showed both sample results and both markers.
Exactly four executor starts and four finishes were recorded: no duplicate
execution. See [approval metrics](verification/approvals-02.json).

### Cancellation and session reset

Each separate request produced one three-call batch: two authorized sampling
commands and one visible, unapproved print proposal. P02-CANCEL used sample
counts 150/75 at one/two-second intervals; P02-RESET used 151/76. The unique
print markers were `P02_CANCEL_MUST_NOT_RUN` and `P02_RESET_MUST_NOT_RUN`.

`/cancel` was admitted at 14:50:56.077052Z. Both executor cancellation events
finished by 14:50:56.252693Z; the local cancellation result followed at
14:50:56.259450Z and its send acknowledgement at 14:50:58.227741Z. `/new` was
admitted at 14:51:45.894215Z; both executor cancellation events finished by
14:51:46.066136Z, new-session result at 14:51:46.072963Z, and send acknowledgement
at 14:51:48.132187Z. Both control replies were visibly inspected.

Each unapproved call is recorded as `not_attempted`, with no executor start.
The interrupted calls retain `unknown` results, rather than claiming their
partial outputs or effects are known. No replay occurred. Service-UID process
inspection after each control found only the runtime and the pre-existing
OpenWA node process: no request-owned workers survived. The trace interval
through 19:36Z contains no late completed result or final send for either stopped
request. See [lifecycle metrics](verification/lifecycle-02.json).

### Benchmark setup

The operator authorized temporary full access for the original hosting benchmark.
An interruption occurred after the initial enablement at 14:52Z and before any
benchmark request; that idle time is not part of the measurement. After resuming,
the unchanged clean note/hash was rechecked, a new session was visibly confirmed,
and the operator again approved enabling full access. The new session command
completed at 19:38:38.519647Z and full access enabled at 19:39:00.429248Z.
The exact original prompt was then sent once, without extra steering.

### Hosting benchmark result

See [sanitized benchmark metrics](verification/benchmark-02.json). All timestamps
in this record with a `Z` suffix are UTC.

| Measure | Prior corrective baseline | This run |
| --- | ---: | ---: |
| Admission to runtime completion | 507.389 s | **393.361020 s** |
| Main model calls | 28 | **15** |
| Reader model calls | 8 | **5** |
| Multi-call main responses | Not compared | **5**, maximum 5 calls |
| Two-minute target | Missed | **Missed by 273.361 s** |

Admission was 19:39:30.009518Z, runtime completion 19:46:03.370538Z,
final send attempt 19:46:04.192500Z, and successful gateway acknowledgement
19:46:06.540409Z (**396.530891 seconds** after admission). The final was visibly
present in the signed-in WhatsApp conversation. Exact recipient receipt time is
not known; the UI's minute label and gateway acknowledgement are not substituted
for it. One progress-send error appears without error details; it did not prevent
the acknowledged, visible final. No final resend was needed.

The observed runtime reduction is **114.027980 seconds (22.47%)**. This is one
live comparison with variable model and web behavior, not a causal estimate or a
latency guarantee. All 27 tool-call results were retained. Four terminal calls
started and finished once with exit zero, no timeout or uncertain effect, and no
unmatched starts. Those four terminal intervals did not overlap each other;
actual concurrent terminal execution is established by the controlled 88.066-second
overlap above, not inferred from multi-call response timestamps. Shared browser
searches still serialize under their tool-owned lock. Batch-result timestamps
are deliberately not treated as individual execution-end times.

Four reader assignments ended: one completed in 31.638 seconds with one model
turn; three reached a limit, taking 191.603 seconds (four turns), 187.536 seconds
(zero turns) and 130.435 seconds (zero turns), including cleanup. The
`release_browser_reader` call at 19:41:50.828654Z did not return until
19:43:30.759139Z, a 99.930485-second observed call-to-result span. These spans
overlap reader/model work and must not be summed into request elapsed time.
Reader time limits, cleanup waits, and remaining latency require separate
performance investigation; this ticket changes no model, reader policy or timeout.

### Saved result and answer quality

Exactly one `edit_vault` call was made at 19:44:24.877717Z; the vault outcome was
`synced` at 19:44:35.583379Z, and its tool result returned at 19:44:35.584114Z.
The final dedicated-vault HEAD and authenticated GitHub `main` both equal
`7b27652211b4e560125919cdee4f368d2b35b0cc`, with a clean working tree. Only
`Technical/Jarvis hosting comparison.md` changed from the starting revision.
Final note SHA-256:
`987bd4796a444cc36d8109f754e04cfafa739eff5d27784a8d88cc05aaeb46b2`.
The diff adds the 2026-11-14 assessment and renames the 2026-11-08 heading from
"Updated" to "Previous"; the older assessment body remains unchanged.

The note and captured evidence were inspected together. The recommendation is to
keep the laptop absent demonstrated sustained overload or a concrete availability
need. It distinguishes the native app, laptop-hosted OpenWA and remote OpenAI
inference; an app-only move retains the laptop gateway dependency, and one VM is
not high availability. Service/memory/load observations are explicitly transient
research-session samples, not long-term capacity evidence. Docker permission
denial is disclosed rather than converting process/listener presence into a
gateway-readiness claim.

The approximately $53.77/month figure is explicitly the historical 2026-11-08
Iowa reference, calculated at 730 hours with a 30 GiB standard disk and in-use
IPv4, not a current Doha quote or full bill. Doha VM/disk/IP and regional egress
remain unverified; backups, taxes, credits and other billable extras are qualified.
Fresh Cloud Run evidence supports separate billing modes and billed minimum
instances, without a workload-price or suitability claim. No cloud resources
were provisioned. The visible 408-character final gives the recommendation,
gateway caveat, regional-price uncertainty and saved-note location.

### Final cleanup and health

`/full-access off` was admitted at 19:46:23.357996Z, completed at
19:46:23.450913Z and acknowledged at 19:46:25.404880Z on November 14. Its
"Full access: off" reply is visible. After resuming on November 15, `/status`
completed at 00:47:46.068107Z and was acknowledged at 00:47:48.223556Z;
the visible reply confirms **no active request, no pending action, full access
off, 15 saved permissions**, and empty context after idle expiry. See
[final control metrics](verification/final-controls-02.json). No further model
calls or terminal work occurred in that final-control interval.

Final service-UID process inspection again found only the runtime and pre-existing
OpenWA node process: no request-owned workers. The service remained active/running
with zero restarts. Installed source and every archived file still matched
`fe70273`; configuration/prompt hashes were unchanged. Gateway readiness was
HTTP 200 and the exact configured named session was `ready`. The service journal
had no warning/error entries from benchmark admission through final verification.
Private WhatsApp screenshot evidence was retained locally, not committed with
unrelated conversation details.

Functional deployment and acceptance passed. The 120-second target remains
unmet; the reader-limit/cleanup observations above are residual performance work,
not authorization to expand this deployment ticket.

## Final review and publication

Independent Standards and Spec reviewers checked `fe70273...02e921b` against the
repository standards and ticket/spec. Standards: zero documented violations and
zero heuristic findings. Spec: zero actionable findings. Both confirmed the
metrics/narrative agree and that the earlier sanitizer/attribution findings remain
resolved. JSON parsing, `git diff --check`, and measurement-helper Ruff lint and
format checks passed. No runtime source changed after the recorded Ubuntu suite.

Acceptance commit `02e921b9f026c32246a9c8626de8e56405a94aad` was pushed to GitHub
`main`; `git ls-remote origin refs/heads/main` returned that exact SHA on
2026-11-15. This published the deployment record, sanitized evidence and updated
operational/domain documentation. Ticket closure records this completed
verification; the active runtime remains `fe70273`, not the documentation commit.
