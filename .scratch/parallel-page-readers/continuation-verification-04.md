# Ticket 04 corrective continuation

Date: 2026-11-08 (Asia/Amman).
Status: complete — functional acceptance passed; performance target missed.

The operator asked to continue through the demonstrated blockers and complete
ticket 04. The [first acceptance record](deployment-verification-04.md) remains
historical evidence of the failed 603.412-second timeout; it is not overwritten
by this continuation.

## Source and scope

Review baseline: `413361e1c32d26271d0e66021d1116ba61360c6a`.
Corrective runtime candidate: `9c2f7d90b867f63cdd536ac0970916ed8143e796`.
Candidate was committed/pushed and GitHub main exact-SHA verified before staging.
Archive SHA-256: `d605b3eae7bf43f0066f492a42179115774fcfab15f57cc9cb14da07e298a40a`.
Operational helpers were subsequently pushed as `a268d1c` and `fa5083a`, with
GitHub main verified and the runtime candidate retained as an ancestor.

Browser observations now describe controls in one synchronous snapshot retained
in a runtime-owned remote object, with references to the actual nodes. An action
obtains only its selected node handle, checks connectivity and reevaluates
research authority. Old references expire on the next observation. This removes
per-control description/disposal round trips without a page-global registry,
model-supplied JavaScript, content truncation or relaxed action guards.

Reopened reader observations explicitly say the current browser is open while
disclosing that prior cookies, storage and tabs were lost. Packaged web guidance
reserves synthesis/save work and bounds research by the decision and remaining
specific gaps. Main model/reasoning and vault-edit implementation are unchanged.

The live terminal working directory still pointed to the archived legacy V1
checkout. A staged configuration changes only that setting to
`/home/samik/Projects/Jarvis/code/current`; parsed before/after configs are compared
after removing that one field. The original config is backed up, and activation
restores both config and release on failure. Activation applied the staged
configuration on 2026-11-08 after the test and screenshot gates passed.

## Diagnosis and red/green verification

The operator confirmed regression seams: public browser, reader-tool and main
request-flow interfaces. The deterministic local fixture exposed slow observation
without external websites or model calls. The 600-control public browser
regression failed its three-second reinspection budget before the fix. The
follow-up regression also failed because no explicit open-browser state was
provided and its old closed-state notice was reused.

On the actual Ubuntu service account, a 60-control diagnostic reproduced the
failure twice: observations took 1.733/1.725 seconds, exceeding a one-second
repeat budget. Measured components included 60 element-description calls taking
1.170 seconds, DOM capture 0.040 seconds, conversion 0.083 seconds, browser launch
1.315 seconds and local navigation 0.091 seconds. This distinguished control IPC
cost from initial navigation and HTML conversion on the fixture.

The old installed reader was also measured against the actual GCP disk page:

| Same Ubuntu host/source URL | Old `551ed85` | Corrected `9c2f7d9` |
| --- | ---: | ---: |
| Initial open through observation | 46.437 s | 9.839 s |
| Observation itself | 39.133 s | 2.255 s |
| Rendered readable characters | 46,968 | 46,968 |

The old observation made 916 element evaluations (25.187 seconds) and 761
disposals (10.230 seconds). The corrected 60-control fixture observes in 0.366
seconds and reinspects in 0.282 seconds, passing the same one-second budget.
These are source-loading measurements, not end-to-end performance acceptance.
No conclusion that every live source delay has the same cause is implied.

Local browser file: **8 passed in 67.90 seconds**. Local skill checks: **23 passed**.
The follow-up test encountered a Windows initial-driver startup timeout on a
separate rerun. Ubuntu candidate tests, including that regression, passed:
**36 passed, 1 skipped in 265.35 seconds**. Focused typechecks, Ruff and formatting
pass. The native deployment target is Ubuntu; no Windows-wide clean suite is claimed.

The complete current-runtime Ubuntu suite passed: **560 passed, 1 skipped in
505.76 seconds**, exit code 0. This includes the explicitly provisioned real
Chrome/ChromeDriver checks. The final pytest process exit was checked after
teardown, before activation.

Installed-candidate screenshot probe as the service account passed with the
existing authorized API key: `gpt-6-luna`, low reasoning, two model turns,
17.359 seconds, correct canvas code and source URL. No model fallback.
All 31 installed Python/skill files and all archived source files match the
verified candidate. Staged configuration and named-session checks pass.
Original config SHA-256:
`9d76db623deef741509923396b36e46c67d5b4ab94ffa8a07a105ebb94f90c59`.
Staged config SHA-256:
`d0efea96c32474876fa58449e50ea12306e836fd3ffb0bcadf5113366c87d9ec`.

## Activation

GitHub main was rechecked as `e5338ef5318acdecd342f9c8619a390eda84ca42`,
which contains the exact immutable runtime candidate `9c2f7d9`. The reviewed
activation helper completed with exit code 0. The active symlink resolves to
`releases/9c2f7d90b867f63cdd536ac0970916ed8143e796`; all archived source and
31 installed Python/skill files match. The service is enabled/active/running,
`NRestarts=0`, listener `172.20.0.1:9011` is present, OpenWA readiness is HTTP
200 and the exact configured named session is `ready`. Prompt hash is unchanged.
The prior release and original configuration backup remain available for rollback.

`systemd-analyze verify` also reported the existing world-writable unit-file
warning and obsolete `CPUAccounting` settings in unrelated system units. These
were not changed by this reader deployment.

Visible WhatsApp `/status` confirmed no active request or pending action and a
fresh context after restart. The previously authorized temporary full-access mode
was enabled for the exact original hosting-and-note benchmark. The existing note
was preserved; its pre-run vault commit is
`a8e0335ee7fcf614b1c1050f99b6ce78bfcd58ff`.

## Original-request acceptance

The exact original prompt was sent once in a fresh session, without recovery
instructions or steering during the measured run. Admission was
`2026-11-08T15:58:01.958039Z`. Runtime disposition was **completed** at
`16:06:29.347275Z`, **507.389 seconds** after admission. It executed one
`edit_vault` call and delivered a concise recommendation to keep Jarvis on the
laptop for now, with the note path and two source links. No generic warning
appendix was added. The final reply was visibly inspected in WhatsApp.

OpenWA's send returned `unavailable`, `may_have_sent=true`; there was no
`openwa_send_result` for this final. A read-only history query matching only the
exact final body independently found its outgoing record with message timestamp
`16:06:33Z` (**511.042 seconds** after admission), row creation `16:06:46Z`
(**524.042 seconds**) and current status **delivered**. These are gateway
timestamps, not a recipient-receipt timestamp. Exact admission-to-recipient
delivery latency is unavailable; the visible result and delivered status are
confirmed. No resend was attempted after the uncertain acknowledgement.

The updated note is `Technical/Jarvis hosting comparison.md`, vault commit
`9005e0e1c6701aa1499baab0cf92a12742abcabb`, committed at `19:06:01 +03`.
Local HEAD and authenticated GitHub `refs/heads/main` matched exactly, with a
clean vault checkout. Note SHA-256:
`f5eadd168a0a223888bf92149d1221ac52aea2a0bc08ed94be96d5a7655b1c98`.
The new assessment is dated 2026-11-08; the earlier assessment is retained under
its own dated heading. This is a successful original-request save/sync, separate
from the historical manual recovery.

### Factual inspection

The actual saved note and captured source observations were inspected; removing
the retired quote gate was not treated as evidence. Relevant claims checked:

| Claim | Evidence and limitation |
| --- | --- |
| Active native service, zero restarts; remote inference | Current service/process/config inspection and current deployment docs; no local inference/GPU claim. |
| 4-core Pentium N3540, 3.7 GiB RAM, 4 GiB swap, 458 GiB root/395 GiB free; about 100 days uptime | Exact live terminal outputs in this run. The note labels resource figures as a point-in-time browser-heavy sample, not sustained capacity or reliability evidence. |
| About 1.09 GB service memory, 2.5 GiB available host memory, 466 MiB swap used, load 2.08/1.47/1.07 | Current terminal sample. No routine-overload or GCP-performance conclusion is drawn from it. |
| OpenWA remains a separate laptop dependency | Current process/listener evidence plus inspected deployment documents. The note discloses its Docker socket permission failure and does not turn a listener into an authenticated session-health claim. Historical 132 MiB/0.01% gateway samples are explicitly historical. |
| E2-standard-2: 2 vCPUs, 8 GiB, $0.06701142/hour | Captured official general-purpose E2 table; nearest region heading is **Iowa (us-central1)**. Doha price is explicitly unverified. |
| Standard disk: $0.000054795/GiB-hour | Captured official general-purpose page's standard provisioned-space table, also headed Iowa. |
| In-use standard-VM IPv4: $0.005/hour | Captured official VPC table, with eligibility caveat for its account free-hour allowance. No claim of a verified Doha total. |
| $48.92 compute + $1.20 disk + $3.65 IP = about $53.77/month | Independently recalculated at 730 hours and 30 GiB, before free-hour credits; explicitly an **Iowa-only reference subtotal** excluding egress, backups, taxes and other services. |

The recommendation is supported as a decision under stated uncertainty: no
current outage or sustained pressure was established; an app-only move retains
the laptop gateway dependency; exact Doha cost and cloud workload speed remain
unknown. The note does not present host uptime as an SLA. Calculator/region/SKU
links are routes for unresolved pricing, not claims that an exact quote was
obtained. Raw note, source observations, screenshots and message traces remain
in ignored private evidence; only reviewed measurements are committed.

### Performance result

| Measure | Earlier completed baseline | Failed initial release | Corrective repeat |
| --- | ---: | ---: | ---: |
| Admission to runtime result | 458.128 s | 600.110 s (timeout) | **507.389 s (completed)** |
| Through transport evidence | 460.284 s acknowledged send | 603.412 s acknowledged send | 511.042 s outgoing message timestamp; 524.042 s row creation; later verified delivered |
| Main model calls | 22 | 49 | **28** |
| Reader model calls | 27 | 8 | **8** |
| Original-request save/sync | Completed | Failed | **Passed** |

The under-two-minute target is **missed**, and the successful repeat is about
49 seconds slower to completion than the old completed baseline. It is not a
speed-success result. Model counts are logical calls, not HTTP attempts.
Completed main-model calls took **361.649 seconds**; main tool spans included
**59.379 s** waiting for readers, **34.057 s** in three searches, **2.357 s** in
five terminal calls and **11.909 s** in the single vault edit. Reader assignment
intervals have a **321.946-second union overlapping main work**; their nested
21.488 seconds of model time must not be added again to end-to-end time.
See [sanitized metrics](verification/hosting-continuation-metrics-04.json).

Two initial readers ran concurrently. The VPC reader completed in 23.957 seconds;
the eventual direct general-purpose-page reader completed in 88.250 seconds.
An overview-page assignment and calculator assignment terminated as time limits,
with terminal trace intervals of 233.694 and 154.671 seconds respectively. The
terminal event is recorded **after browser cleanup**; those intervals do not
isolate research from teardown and do not establish continued research after
the 120-second assignment deadline. They also do not establish a 120-second
total wall-time bound. Main polling and slow main-model responses remain material
costs. Source-startup versus cleanup in the remaining long intervals needs a
focused follow-up; changing the host or provider is not established as a fix.

## Lifecycle and remaining follow-up

Progress appeared at 27.324 seconds, then visibly reported two running readers.
One progress send had an uncertain/error acknowledgement even though its text
was visible. A live follow-up attempt on a running reader correctly returned
`reader_busy` with no queue. Successful retained/reopened follow-up behavior is
covered by the focused Ubuntu regressions; the prior live follow-up evidence
remains in the historical record.

Full access was visibly turned off after the benchmark. A separate request
started two readers and waited for their findings. Visible progress showed two
running, then one running/one completed. `/cancel` was admitted at
`16:09:12.076962Z`, acknowledged locally at `16:09:12.145598Z`, the active reader
finished cancelled/closed at `16:09:13.118999Z`, and the cancellation reply was
acknowledged sent at `16:09:14.487934Z`. No final reply from the cancelled request
was sent. At `16:13:02Z` process inspection found only the runtime and the
separate existing OpenWA `node dist/main` process for that UID: **zero reader
browser/driver processes**. Service remained active/running with zero restarts.
The final visible `/status`, captured at `16:15:52Z`, showed **Active request:
none; pending action: no; Full access: off; saved permissions: 15**, with no late
research reply. See [lifecycle metrics](verification/lifecycle-continuation-04.json).

The journal contains one remaining timeout/close-race diagnostic during the
benchmark: Python 3.14 logged `AttributeError exception in shielded future` when
`observe()` accessed `self._page.url` after concurrent close cleared the page.
The shielded action is drained and no worker survived, but this is **not clean
exception handling**. The concrete race, regression needed, long terminal
intervals and remaining performance work stay open in [ticket 05](issues/05-fix-live-hosting-completion.md).
They are not described as fixed by functional acceptance of ticket 04.

## Review

### Standards

Independent review found one P2 operational issue: rollback source verification
still used the candidate's interpreter/archive. `fa5083a` passes the release,
archive and config together, then verifies a restored release with its own
interpreter and archive. Independent rereview confirmed resolved; **0 remaining
documented violations or actionable heuristic findings**.

### Spec

Independent source and operational review found **0 actionable findings**. It
confirmed actual-node identity and action checks, disclosure of reopened state,
full readable content, configuration staged before activation and rollback of
both configuration and release. That review did not treat pending live checks
as proof of success.

After the live repeat, independent spec review confirmed ticket 04 can close
with the successful original-request save/delivery, factual inspection and
cleanup evidence, while reporting the missed performance target. It required
preserving the unknown recipient receipt time, distinguishing terminal intervals
from assignment work, and explicitly keeping the close-race diagnostic open in
ticket 05. Those limitations are recorded above.

### Final documentation review and repository closeout

Both independent axes reviewed `e5338ef...5997c08`, the nine-file acceptance
documentation/metrics change. **Standards: 0 documented violations, 0 actionable
heuristic smells. Spec: 0 actionable findings.** The reviews confirmed the
functional/performance distinction, saved-note evidence, timestamp limitations,
and explicitly open residual work. No raw private evidence or debug code is in
the change. JSON parsing and `git diff --check` passed; no runtime code changed
after the candidate's successful full suite.

The acceptance and final review records are committed and pushed on `main`, with
the final GitHub branch SHA checked against local HEAD. The working tree is
clean. The active immutable runtime remains `9c2f7d9`; documentation commits do
not silently replace the installed source.
