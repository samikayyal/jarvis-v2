# 02: Deploy and measure parallel tool calls in WhatsApp

**What to build:** Make the verified parallel main-tool behavior available in the
Ubuntu service, demonstrate that approvals remain usable while authorized work
runs, and measure the original hosting-and-note workflow against the recorded
baseline with its saved result and answer quality intact.

**Blocked by:** 01 — Execute parallel tools while approvals remain interactive.

**Status:** complete

- [x] Commit and push the exact intended candidate and verify that GitHub
  contains that SHA before activation. Stage/build that source, verify installed
  source identity, and complete the required Ubuntu checks. If push or remote
  verification fails, leave the active release unchanged. Retain a verified
  rollback route.
- [x] After activation, confirm the configured service and messaging gateway
  session are ready and an authorized WhatsApp request receives its expected
  reply. Record the active commit and configured batch maximum.
- [x] Use controlled live requests to demonstrate concurrent authorized work,
  an approval prompt arriving before that work finishes, and a second approval
  arriving while an earlier approved operation is still running. Verify exact
  actions, retained sibling results and absence of duplicate execution.
- [x] Exercise cancellation and session reset with running siblings and a
  visible approval. Confirm cleanup, no late final reply and no surviving
  request-owned workers. Record final idle/pending/full-access state. Preserve
  uncertain transport or side-effect outcomes rather than blindly retrying.
- [x] Repeat the original laptop-versus-GCP request with a fresh session and
  comparable starting note. Verify a supported recommendation, precise regional
  price uncertainty, successful note save/sync and a short final WhatsApp reply.
- [x] Record main/reader model-call counts, observed parallel batches, actual
  execution overlap, runtime completion and available delivery evidence.
  Compare against the recorded 507.389-second, 28-main-call corrective baseline;
  do not add overlapping durations or equate gateway timestamps with recipient
  receipt time.
- [x] Report functional correctness and latency separately, including whether
  the two-minute target was met. A missed target is not permission to expand
  this ticket into model tuning, reader-polling redesign or unrelated diagnosis;
  record any residual performance work explicitly.
- [x] Commit and push the deployment and acceptance records, verify their
  presence on GitHub, and keep the source/deployment status in the domain and
  operational documentation current. Mark this ticket complete only when the
  deployment and evidence requirements above are satisfied.

## Comments

Completed 2026-11-15. Runtime `fe70273` is deployed with a batch maximum of five.
Live WhatsApp overlap, successive approvals, retained results, cancellation/reset
cleanup and final idle/full-access-off checks passed. The hosting benchmark
completed in 393.361 seconds with 15 main and 5 reader calls, saved/synced the
note and visibly delivered its short final. The two-minute target was missed;
reader-limit/cleanup latency remains explicit separate follow-up work.

See the [deployment and acceptance record](../deployment-verification-02.md).
Independent Standards and Spec reviews passed with no remaining findings.
Acceptance commit `02e921b9f026c32246a9c8626de8e56405a94aad` was pushed and
exact-SHA verified on GitHub before closure.
