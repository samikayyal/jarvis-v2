# 31 — Supervise real Google and knowledge-vault acceptance

**What to build:** Under direct human supervision and exact production approvals, demonstrate the bounded real Google and knowledge-vault behaviors without treating mocks as production proof.

**Blocked by:** 30 — Supervise initial activation and OpenWA handoff acceptance.

**Status:** ready-for-human

## Sanitized supervised evidence — 2026-08-16

- The Ticket 31 repair was deployed from the reviewed worktree and the repair branch is synchronized with its remote branch by fast-forward. The live release reports the pinned source revision; all 14 deployed containers are healthy.
- OpenWA remains independently deployed with the named `jarvis` session `ready`; both Jarvis workers are `ready`, the audit directory is writable, resource pressure is `ok`, and the OpenWA bind/firewall remain limited to the reviewed private-LAN boundary.
- Baseline Google OAuth was reconnected with the fixed read scopes and the configured identity match. Bounded Gmail, Calendar, and Drive reads passed; disconnect/reconnect behavior passed; unsupported binary Drive export was refused.
- Gmail altered approval/rejection passed. One exact supervised Gmail send completed through the broker, the exact sent message was verified, and an old approval replay produced no second send. Calendar altered approval/rejection passed. One exact supervised Calendar event completed through the broker, material fields were verified, the old approval replay produced no second event, and the stale-generation proposal was rejected without a write.
- Excluded Gmail deletion, Calendar deletion, Drive modification/upload, vault deletion/rename, non-Markdown, hidden/plugin/configuration, attachment, nested-repository, outside-directory, and force-push/rebase/history-rewrite/conflict operations were each refused without a proposal or dispatch.
- A deterministic bounded vault read passed. The exact Markdown write row is blocked: the connector exposes only four lines per excerpt, and the model produced proposals that removed existing lines; each malformed proposal was rejected, so no vault content changed. The dedicated clone remains clean and synchronized with its remote base.
- Unknown-outcome acceptance rows were not manufactured: the runbook requires a separately reviewed interruption method, and none was available for safe use. No containers were killed, traffic was interrupted, or firewall/proxy mutation was attempted.
- Final durable reconciliation: no active request, no pending session action, no outbound conversation outbox rows, no unresolved dispatch attempt, no duplicate Gmail side effect, no duplicate Calendar side effect, and no vault dirty state. Protected audit/trace evidence remains on-host and is not copied here.

The ticket remains `ready-for-human` pending a reviewed unknown-outcome procedure and a safe exact Markdown write path. Do not mark `complete` until every worksheet row passes.

## Sanitized supervised evidence update — 2026-08-16

- The latest repaired release was built, cut over, and passed the administrative gates: all 13 Jarvis services are healthy and ready, audit is writable, the named OpenWA session is ready, the reviewed private bind and network memberships remain intact, and the post-change nightly backup passed.
- The fresh exact-path vault read now reported complete content and a final newline through the broker metadata. The resulting visible write proposal was still malformed: its rendered unified diff contained no hunk and no requested acceptance-line addition. It was rejected with the displayed exact control grammar; no approval, write, commit, push, or unknown outcome occurred.
- The Chrome `/status` reconciliation after rejection reported no active request and no pending action. The dedicated vault clone remains clean and equal to its remote base, and no duplicate side effect was observed.
- The exact Markdown write row therefore remains blocked. The Gmail/Calendar unknown-outcome rows also remain blocked because the runbook requires a separately reviewed post-dispatch interruption method; no container, traffic, firewall, or proxy interruption was attempted.

## Sanitized supervised evidence update — vault dispatch reconciliation — 2026-08-16

- A fresh exact vault proposal passed protected structural comparison: one canonical path, the current content preserved byte-for-byte, one appended marker line, zero removed diff lines, one added diff line, fixed subject, and configured identity shape.
- The exact one-time approval was sent through the existing Chrome Jarvis chat. The connector committed locally but returned an outcome-unknown push result. Authoritative remote reconciliation shows the remote head unchanged; the local clone is clean but one commit ahead and contains the labeled marker. No remote vault side effect or duplicate commit was established.
- The connector is now in its documented manual-recovery block. No manual Git push, retry, reset, deletion, or history rewrite was attempted. The session was reconciled to no active request and no pending action.
- Protected trace timing places the worker failure at the connector's 20-second write deadline; it records no authentication or explicit remote-rejection signal. This is a diagnostic lead for human recovery, not evidence that the unpushed commit may be retried.
- The exact vault row remains blocked, and the ticket remains `ready-for-human`. A human must resolve the isolated local-ahead/manual-recovery state under the vault runbook before any further vault instruction.

## Sanitized supervised evidence update — vault clone restoration — 2026-08-16

- Following the documented human recovery direction, the dedicated clone was restored locally to its authoritative `origin/main` head. The local and remote-tracking heads now match at `c59eadab`, and the clone is clean on its attached `main` branch.
- The previously local-only labeled commit is no longer present; no private remote mutation, duplicate commit, or note change remains. The connector process was not restarted or bypassed, so its in-memory manual-recovery latch still requires the documented administrative recovery before another vault write instruction.
- The vault exact-write row remains blocked and the ticket remains `ready-for-human`.

- [ ] Real bounded Gmail, Calendar, and Drive reads succeed with the configured identity and fixed scopes, including supervised failure and reconnection cases.
- [ ] One reversibly labeled Gmail send or reply and one reversibly labeled Calendar mutation execute only through exact approval, with altered/replayed/unknown-outcome behavior checked safely.
- [ ] A real deterministic vault read and one exact approved Markdown commit and normal push succeed; excluded and destructive capabilities are tested only through rejection.

