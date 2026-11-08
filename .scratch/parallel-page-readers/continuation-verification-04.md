# Ticket 04 corrective continuation

Date: 2026-11-08 (Asia/Amman).
Status: source staged; full suite and live repeat pending.

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
restores both config and release on failure. No live config change is claimed yet.

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

Installed-candidate screenshot probe as the service account passed with the
existing authorized API key: `gpt-6-luna`, low reasoning, two model turns,
17.359 seconds, correct canvas code and source URL. No model fallback.
All 31 installed Python/skill files and all archived source files match the
verified candidate. Staged configuration and named-session checks pass.
Original config SHA-256:
`9d76db623deef741509923396b36e46c67d5b4ab94ffa8a07a105ebb94f90c59`.
Staged config SHA-256:
`d0efea96c32474876fa58449e50ea12306e836fd3ffb0bcadf5113366c87d9ec`.

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
full readable content, staged-only configuration migration and rollback of both
configuration and release. Pending activation/live acceptance is not treated as
proof of success.
