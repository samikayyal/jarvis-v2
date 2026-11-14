# 02: Deploy and measure parallel tool calls in WhatsApp

**What to build:** Make the verified parallel main-tool behavior available in the
Ubuntu service, demonstrate that approvals remain usable while authorized work
runs, and measure the original hosting-and-note workflow against the recorded
baseline with its saved result and answer quality intact.

**Blocked by:** 01 — Execute parallel tools while approvals remain interactive.

**Status:** ready-for-agent

- [ ] Commit and push the exact intended candidate and verify that GitHub
  contains that SHA before activation. Stage/build that source, verify installed
  source identity, and complete the required Ubuntu checks. If push or remote
  verification fails, leave the active release unchanged. Retain a verified
  rollback route.
- [ ] After activation, confirm the configured service and messaging gateway
  session are ready and an authorized WhatsApp request receives its expected
  reply. Record the active commit and configured batch maximum.
- [ ] Use controlled live requests to demonstrate concurrent authorized work,
  an approval prompt arriving before that work finishes, and a second approval
  arriving while an earlier approved operation is still running. Verify exact
  actions, retained sibling results and absence of duplicate execution.
- [ ] Exercise cancellation and session reset with running siblings and a
  visible approval. Confirm cleanup, no late final reply and no surviving
  request-owned workers. Record final idle/pending/full-access state. Preserve
  uncertain transport or side-effect outcomes rather than blindly retrying.
- [ ] Repeat the original laptop-versus-GCP request with a fresh session and
  comparable starting note. Verify a supported recommendation, precise regional
  price uncertainty, successful note save/sync and a short final WhatsApp reply.
- [ ] Record main/reader model-call counts, observed parallel batches, actual
  execution overlap, runtime completion and available delivery evidence.
  Compare against the recorded 507.389-second, 28-main-call corrective baseline;
  do not add overlapping durations or equate gateway timestamps with recipient
  receipt time.
- [ ] Report functional correctness and latency separately, including whether
  the two-minute target was met. A missed target is not permission to expand
  this ticket into model tuning, reader-polling redesign or unrelated diagnosis;
  record any residual performance work explicitly.
- [ ] Commit and push the deployment and acceptance records, verify their
  presence on GitHub, and keep the source/deployment status in the domain and
  operational documentation current. Mark this ticket complete only when the
  deployment and evidence requirements above are satisfied.
