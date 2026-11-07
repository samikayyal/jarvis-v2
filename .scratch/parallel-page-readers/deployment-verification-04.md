# Ticket 04 deployment and live acceptance

Date: 2026-11-07 (Asia/Amman).
Status: deployed; original end-to-end acceptance failed; ticket remains open.

## Exact source and rollback

Candidate: `551ed852b16f17f87e149feae1690da39114fc5b` on `main`.
`git push origin main` succeeded; `git ls-remote origin refs/heads/main`
returned that exact SHA before staging. The source archive SHA-256 is
`f1f5c0489be344924da8855e206a8cba5a86116e25262e5fc5c11e144fc29213`,
verified after transfer. All archived files and all 31 installed Python/skill
files match it byte for byte; the retired `page_reader.py` is absent.

Staged path: `/home/samik/Projects/Jarvis/code/releases/551ed852b16f17f87e149feae1690da39114fc5b`.
Previous/rollback: `/home/samik/Projects/Jarvis/code/releases/39765480870245a87abc6ee5d9d689af5d6f8aee`.
Before activation the previous service was active/running with zero restarts,
OpenWA was healthy, its exact configured named session `jarvis` was ready,
and visible `/status` confirmed idle, no pending action and full access off.

## Ubuntu provisioning

Ubuntu 26.04 LTS x86-64; Python 3.14.4. Installed the hash-locked deployment
requirements and non-editable package from the verified archive. A resumed
`uv venv` initially attempted to remove the existing environment and failed
on generated files. That inactive environment was retained as `.venv.incomplete-04`
and replaced with a fresh locked installation before the successful probes.
The staging helper now preserves an existing environment.

Playwright 1.58.0 predates Ubuntu 26.04 and rejects automatic provisioning for
that platform. Explicit `PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64`
selects its vendor Chromium 145.0.7632.6 / revision 1208, headless-shell 1208
and FFmpeg 1011 artifacts. `install-deps chromium` installed their Ubuntu
libraries/fonts. This is verified host compatibility, not a claim that
Playwright 1.58 officially supports Ubuntu 26.04.

The browser cache belongs to the service account under
`other/runtime/.cache/ms-playwright`. Tests point to that same cache.
Initial service-account startup failed because Ubuntu restricted anonymous
user namespaces. The committed AppArmor profile grants `userns` only to the
two exact revision-1208 binary paths, following Ubuntu's existing Chrome
profile pattern. The global `kernel.apparmor_restrict_unprivileged_userns=1`
and Chromium sandbox remain enabled. No OpenWA recreation or firewall change
was made.

Installed browser binary SHA-256 values:

- Chromium: `481fea1516a1f2b76454664272f12cd9dd1f20117b21e1f1498e08bc7f872c00`.
- Headless shell: `27e55cc30e487493b89b663085d6deeb0707be201a568657c922fac9076c317c`.

Installed-package public-browser probe as UID 997: IANA initial reading,
retained findings, fresh browser follow-up and browser/profile cleanup pass.
The independent Selenium Google search returned `unavailable` after the
probe's 20-second default operation timeout; no successful search claim is made.
The live runtime keeps its existing 60-second search-operation timeout.

Authorized reuse of the existing key: installed-candidate screenshot probe
using the existing canvas fixture passed with `gpt-6-luna`, low reasoning,
two model turns, **17.60 seconds**, returning `ORCHID 7391` and the fixture URL.
The original billed fixture first returned `running` at its one 30-second
wait and failed its completion assertion. The operational probe polls the
public wait operation within the configured 120-second assignment budget;
no runtime or test-source changes were made to hide that observation.

Configuration check passes without migration: the live `[web_reader]` only
names model and reasoning. Defaults are 3 active assignments, 3 browser
contexts, 8 retained readers, 24 model turns, 120 seconds, 128000 context tokens
and 8000 output tokens. Main remains `gpt-6-luna`, medium reasoning.
Configuration SHA-256:
`9d76db623deef741509923396b36e46c67d5b4ab94ffa8a07a105ebb94f90c59`.
The existing prompt matches the candidate example and needs no replacement;
SHA-256 `9beb38f5530a9bf10fe3159576ee016957ca7227622a2530fd22ca172d3d3db1`.

## Checks and remaining gates

Focused typechecks, Ruff and formatting pass for candidate source and operational
Python helpers. Initial concurrent/browser/configuration run, begun before the
AppArmor fix, reported 105 passed and 8 failed. Seven failures were browser
startup; the three-reader test hit its provider-arrival timeout. The clean
post-provisioning browser file passed: **7 passed in 65.32 seconds**.
Exact-candidate Ubuntu full suite: **559 passed, 1 skipped in 514.93 seconds**.
Chromium/ChromeDriver and POSIX fixtures were enabled. The one skip is the billed
screenshot fixture; the separately authorized installed-candidate probe passed.
No runtime-source change followed this suite.

## Preactivation review

Review baseline: `551ed852b16f17f87e149feae1690da39114fc5b`.
Reviewed operational commits: `b4e12bf` and `82badfb`.
Both were pushed before activation; GitHub main was verified at
`82badfb5f8cafd542009ea0b281fa986e2c09a67`, containing the exact runtime
candidate as an ancestor. The transferred runtime source remains `551ed85`.

### Standards

The initial independent review found rollback did not repeat the documented
health gates and an interrupted activation could resume after rollback. The
replacement symlink is now prepared before stopping the service; an EXIT guard
covers subsequent errors, signals terminate through recovery, and rollback
rechecks service, restart count, enabled state, exact symlink, listener,
configuration and gateway/session readiness. Independent rereview confirmed
both findings resolved: **0 remaining documented violations or actionable smells**.

### Spec

The independent review found one P2 in the same stop/switch interval: a failed
symlink command could leave the prior release's service stopped. Independent
rereview confirmed the EXIT guard resolves it and found no new spec violations.
The bounded trace helper separately counts main/reader calls and marks reader
model time as nested within reader wall time. **0 remaining findings**.

Shell syntax and rendered systemd-unit validation pass. Systemd reported the
pre-existing world-writable unit mode and unrelated deprecated CPUAccounting
settings as warnings; no unit contents or permissions were changed.

The first activation attempt at 17:09 Asia/Amman restored the previous release
after the health check's fixed three-second startup wait. Its rollback health
check also ran too early. Journal inspection showed no process failure: the
restored runtime opened its listener nine seconds after systemd's Started line,
and subsequently had zero restarts. Commit `dfbab37` changes activation and
rollback to wait up to 45 seconds for the actual listener. It was pushed and
GitHub main verified at `dfbab37722117647e7ea82ce770e3c832bdb09dd`
before the next activation attempt. The source candidate remains `551ed85`.

The next activation succeeded. Post-activation source verification still matches
all archived files and all 31 installed runtime/skill files. `current` resolves to
the candidate, the runtime is enabled and active/running with zero restarts,
the bridge listener is open, and OpenWA's configured `jarvis` named session is
ready. Configuration and prompt hashes are unchanged. The prior release remains
available for rollback. Visible `/status` after restart confirmed a fresh, idle
working session with full access off. The operator explicitly authorized turning
full access on for the benchmark to match the baseline, then off afterward.

## Original visible WhatsApp benchmark: failed

The exact ticket prompt was sent in the authorized Jarvis conversation in Chrome
WhatsApp Web, with a fresh deployed working session and explicitly authorized full
access on. No API/webhook substitute was used. Admission was
**17:16:15.130 Asia/Amman**; the runtime timed out at **17:26:15.240** and the
timeout reply was acknowledged at **17:26:18.542**. A visible `/status` during
work remained responsive and reported processing/full-access-on. Full access was
turned off after the timeout and visibly acknowledged off.

| Measurement | Prior completed baseline | This request |
| --- | ---: | ---: |
| Admission to completion/result | 458.128 s | **600.110 s, timeout** |
| Admission through delivery | 460.284 s | **603.412 s, timeout reply** |
| Main model calls | 22 | **49** |
| Reader model calls | 27 | **8** |
| Full hosting result saved in the original request | Yes | **No** |

The original request made **no `edit_vault` call**. A read-only vault check
confirmed the comparison note absent before recovery. Thus neither functional
end-to-end acceptance nor the under-two-minute target passed. Fewer reader model
calls do not establish a speed improvement. The timeout reply contained the
runtime's interruption notice, not the requested short recommendation.

[Sanitized metrics](verification/hosting-metrics-04.json) are bounded to the
original message's admission/delivery interval. Recovery and later checks are
excluded. Logical calls total 57 and HTTP attempts total 57; the last main call
had no completed output before cancellation. Available timings:

- Completed main-model calls: **303.037 s**; unfinished last-call local wait:
  **27.276 s**. This does not claim provider-side cancellation.
- Main searches: **91.283 s** across 3 calls. Main reader waits: **153.779 s**
  across 9 calls. Eight terminal calls themselves took **5.551 s**, although
  their large outputs contributed to later main-model context.
- Reader assignment intervals overlap main work; their wall-time union was
  **397.117 s**. Reader model time was **27.723 s**, nested within those reader
  intervals. Do not add reader wall/model totals to the sequential main timings.
- Nine reader start calls, eight status collections, five explicit releases and
  one retained-reader follow-up. The first three concurrent GCP assignments
  reached time limits after **120.93–127.28 s**, before any reader model call.
  Later source reads succeeded. The calculator assignment also reached its limit.
  Phase-level evidence is insufficient to identify launch, navigation, settling,
  extraction, networking or host contention as the cause.

Progress was visible at about **28.18 s** after admission, then on reader-state
changes, including three running readers. Seven progress sends were acknowledged
in traces; one additional progress send recorded an error/uncertain acknowledgement.
The corresponding state appeared in the UI; do not blindly retry an uncertain
send or claim every progress message was acknowledged. No source-warning appendix
was added to model answers.

## Recovery note and factual inspection: separate outcome

The operator authorized only the benchmark's temporary full access; all recovery
ran with full access off. A follow-up requested synthesis from retained evidence
without more research. Its first frozen proposal was rejected, because it called
a 17:25 resource sample post-timeout (the request timed out at 17:26) and mixed
legacy unactivated V1 deployment material into migration guidance. A revised
single-file creation was reviewed visibly and approved once with `1`; no saved
permission was added.

`Technical/Jarvis hosting comparison.md` was created and synced as vault commit
**`a8e0335ee7fcf614b1c1050f99b6ce78bfcd58ff`**. Read-only local Git status was
clean, and `git ls-remote` using the configured pinned vault SSH identity returned
that same SHA for GitHub `main`. This verifies Git-backed vault sync; no separate
Obsidian desktop-client rendering check is claimed.

The approved write executed before the next continuation reached the working
session's context limit. WhatsApp reported the context limit rather than a saved
note confirmation. A subsequent fresh-session read, without edits/research,
visibly returned: “Yes—the note is present at Technical/Jarvis hosting comparison.md.
It is synced.” This recovery does **not** turn the original timeout into a pass.

The final note was reread from the actual vault file. Its recommendation is to
keep the native Ubuntu service for now while distinguishing untested cloud
benefits and the local OpenWA dependency. Important checks:

- Host counts/capacities and the service identity agree with the session's
  terminal observations. The 17:25 memory/process figures are explicitly a sample
  during research, with no claim of normal/peak load or measured GCP advantage.
- The actual reader input from Google's primary pricing page contains the
  `e2-standard-2` row: 2 vCPUs, 8 GiB, **USD 0.06701142 per hour**. The note keeps
  this published Iowa reference distinct from a Doha bill. Reviewer inspection
  of the primary table, not removal of quote validation, supports this check.
- The primary Google regions table lists Doha `me-central1` zones and E2. An
  independent primary-page lookup confirmed those rows. This establishes region
  availability, not price or measured latency.
- Doha compute/disk/IPv4/egress/backup/tax/free-tier totals and laptop power cost
  remain specifically unverified. The note gives no invented monthly quote.
- Historical OpenWA resource/readiness records are labelled historical; the note
  does not claim those old samples are a current authenticated session check.
- The legacy V1 deployment section/reference was removed from the saved note.

## Lifecycle checks and remaining work

The original run exercised a retained-reader follow-up and explicit releases.
Post-timeout process inventory found no reader Chromium or Playwright driver;
only the native runtime and pre-existing Node companion remained. The live
journal also contained an unretrieved `TargetClosedError` future during reader
cleanup, recorded for follow-up rather than hidden by the process-health check.

The visible cancellation check started two concurrent research readers. An initial
start-only acknowledgement ended its containing request and closed its readers;
that was completion cleanup, not a live cancellation pass. The next request
explicitly waited for findings. Two running readers were observed, then one had
completed while the other remained running when `/cancel` was sent. The visible
acknowledgement said cancelled locally and full access off. The trace records
`/cancel` admitted at **17:49:37.190**, runtime cancellation at **17:49:37.238**,
the active reader cancelled/closed at **17:49:37.558**, the old main result ignored,
and cancellation acknowledgement delivered at **17:49:38.950**. The completed
reader's findings remained available, while its browser closed with the request.
No final reply for the cancelled request appeared afterward. The final visible
`/status` at **17:54** showed no active request, no pending action, 15 saved
permissions and full access off. No late research reply appeared through 17:55,
more than five minutes after cancellation. This observes local delivery safety;
it does not claim confirmed provider-side cancellation.

Final host check at **17:55:06** again matched all 31 installed files to `551ed85`,
found runtime enabled/active with zero restarts and the exact gateway session
ready, and found only the runtime plus pre-existing Node companion under the
service account: **no reader Chromium or Playwright driver processes**.
Configuration/prompt hashes still match the predeployment values.

The next work is [ticket 05](issues/05-fix-live-hosting-completion.md): establish
source-load phase timings, bound main orchestration/research, correct the context
route, preserve completion/save budget, and test lifecycle/context-limit reporting.
No vault-edit redesign was attempted. Ticket 04 remains open until a fresh repeat
of the original scenario actually completes and saves/syncs its note; the initial
under-two-minute target must still be assessed separately from functional success.

## Delivery

Operational changes, final measured outcomes, sanitized evidence and follow-up
scope were committed to the current `main` branch and pushed. GitHub main was
verified at `b1cf9b01d7584f0a7ebf228438b29bf623edb4d4` after that evidence push;
the final documentation-only commit records this result and the issue checklist.
The deployed runtime source remains immutable at `551ed85`. Sensitive live trace
extracts and WhatsApp screenshots remain in ignored `private/`, outside Git.
