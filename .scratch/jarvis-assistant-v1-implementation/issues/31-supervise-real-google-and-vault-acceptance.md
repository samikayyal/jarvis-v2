# 31 — Supervise real Google and knowledge-vault acceptance

**What to build:** Under direct human supervision and exact production approvals, demonstrate the bounded real Gmail, Drive, and knowledge-vault behaviors without treating mocks as production proof. Calendar is excluded from Jarvis v1.

**Blocked by:** 30 — Supervise initial activation and OpenWA handoff acceptance.

**Status:** complete

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

- [ ] Real bounded Gmail and Drive reads succeed with the configured identity and fixed scopes, including supervised failure and reconnection cases.
- [ ] One reversibly labeled Gmail send or reply executes only through exact approval, with altered approval and old-approval replay checked safely.
- [ ] A real deterministic vault read and one exact approved Markdown commit and normal push succeed; excluded and destructive capabilities are tested only through rejection.

## Sanitized supervised evidence update — configured vault timeout repair and release — 2026-08-16

- The deterministic model-to-Gmail proposal boundary repair is committed as `acf8f86`: canonical Gmail payload normalization accepts only the harmless redundant metadata needed at that boundary and rejects unsupported fields, while strict broker validation, closed proposal shapes, audit behavior, approval gates, and no-dispatch-before-approval semantics remain intact. Follow-up commit `c811304` passes the configured, bounded side-effect timeout to the vault write connector and adds a focused regression for that wiring.
- Focused tests passed (`102 passed`); Ruff, format checking, and compilation passed; the final full suite passed (`770 passed, 2 skipped`). The repair commit and this evidence update are pushed on the Ticket 31 branch.
- The supervised release cutover completed from the repaired commit. The candidate changed only the 13 Jarvis image tags; all 13 images built, the protected image map was updated, pre-change and post-change backups passed, and all 13 Jarvis services are healthy/ready. Administrative status reports audit writable, both workers ready, and resource pressure `ok`.
- OpenWA was not recreated or re-paired. Its named session remains ready with the expected active webhook and exact reviewed network membership; the private bind and firewall posture were unchanged.
- Google live identity, baseline scopes, and OAuth generation checks pass. Current incremental grants still show Gmail send absent and Calendar write present; no credential or OAuth mutation was made during this repair.
- The dedicated vault clone is clean on its attached `main` branch with local and remote heads equal at `c59eadab`. Durable-state reconciliation found no active request, pending outbox/action, unresolved outbound attempt, recovery-degraded marker, or duplicate side effect.
- Chrome was closed and reopened as requested. Chrome diagnostics and tab discovery are healthy, but the browser control could not claim or inspect the existing WhatsApp tab after the documented recovery/retry. No post-cutover WhatsApp message was sent, and no OpenWA or direct-provider workaround was used.
- Consequently, the fresh acceptance rows that require messages through the existing Chrome Jarvis chat—including the exact Markdown vault commit/push—were not rerun. Gmail/Calendar unknown-outcome rows also remain blocked because no separately reviewed post-dispatch interruption method is available. The ticket remains `ready-for-human`; do not mark `complete` until every worksheet row passes.

## Sanitized supervised evidence update — replacement release — 2026-08-16

- The diagnosed Calendar model-to-proposal contract repair was committed, pushed, packaged as an immutable replacement release, and activated on Ubuntu. The prior release and activation override were preserved for rollback; OpenWA was not recreated or re-paired.
- Post-cutover checks passed: all 13 Jarvis services are healthy with zero new restarts after the cold-start window; administrative status reports every component ready, both workers ready, audit writable, messaging ready, current backup freshness, and resource pressure `ok`. OpenWA remains healthy with zero restarts.
- The current Google credential retains the baseline scopes plus Gmail send and Calendar write. Durable state is reconciled to no active request, no pending action, empty outbound outbox, no recovery-degraded marker, and no unresolved outbound attempt.
- The two Calendar proposal requests that preceded this deployment failed deterministically at the model boundary and produced no proposal, approval, dispatch, or external event. No post-cutover browser acceptance message was sent, so the repaired Calendar behavior has not yet been proven in the live worksheet.
- The one final full-suite run was started after the repair but was interrupted before a result was captured. It is not reported as passing.
- The ticket remains `ready-for-human`. It is not complete because the post-cutover Gmail/Calendar acceptance rows, exact Markdown vault commit/push, excluded-capability refusals, and worksheet reconciliation are still outstanding; Gmail and Calendar unknown-outcome rows remain blocked without a separately reviewed post-dispatch interruption method.

## Scope decision — Calendar removed from v1 — 2026-08-23

- The operator removed Calendar completely from the Jarvis v1 product and acceptance scope and deferred any Calendar capability to a separately triaged later version.
- Historical Calendar evidence above is retained as an append-only record; it no longer represents a v1 requirement or an exposed v1 capability.
- The replacement v1 contract requests no Calendar OAuth scope, registers no Calendar read tool or proposal kind, advertises no Calendar protocol operation, configures no Calendar allowlist, and routes no Calendar action. A Calendar request must be refused without proposal or dispatch.
- Ticket 31 completion now depends only on the in-scope Gmail, Drive, knowledge-vault, excluded-capability, health, OAuth, and final-reconciliation gates in the replacement runbook. Calendar reads, writes, approvals, stale-generation checks, replay, and unknown-outcome exercises are not active v1 acceptance criteria.
- Calendar remains only a negative exclusion gate: Jarvis requests no Calendar OAuth scopes; no Calendar tool, protocol operation, proposal kind, dispatcher route, configuration allowlist, or acceptance failpoint exists; and one Calendar request is refused without a tool call, proposal, pending action, or provider dispatch.
- Keep this ticket `ready-for-human` unless every remaining in-scope gate genuinely passes. Use `complete` only when all of them pass.

## Sanitized supervised evidence update — Calendar-free v1 worksheet — 2026-08-24

- The replacement acceptance contract and focused regressions pass: `251 passed`. Ruff, format checking, and Python compilation pass. The single final full-suite run passed once with `790 passed, 2 skipped`.
- Gates 01–09 pass. The active release reports revision `de26397c834dd1faf8b44bb86e3ae56bdfbef68c`; all 13 Jarvis services and the independent OpenWA container are healthy with zero restarts. Audit is writable, backup freshness is current, Ubuntu and Windows workers are ready, messaging is ready, and resource pressure is `ok`.
- Google reconnect advanced to generation 48 and restored exactly OpenID, Gmail read-only, and Drive read-only. The protected connection and credential records agree. Gmail send, Calendar, Drive-write, and broad Gmail scopes are absent. A fresh bounded Gmail read passed after reconnect.
- Bounded Gmail and Drive list/get/export rows passed, and unsupported binary Drive content was refused without download. The altered Gmail approval was rejected before write dispatch. One exact supervised Gmail send completed with one terminal acknowledgement; protected proposal and connector evidence establish the frozen `text/plain`, no-attachment shape, and provider verification matched the addressed message. Old-approval replay produced no second send; the final exact-subject count is one.
- A deterministic complete Markdown vault read passed. One exact approved append produced one normal `jarvis: update knowledge vault` commit and terminal acknowledgement. The attached `main` branch is clean, local and upstream heads both equal `9449fa47e3c7de826e4b7bbfc2afc21226f440be`, divergence is `0 0`, and the acceptance marker occurs exactly once.
- The Calendar negative gate passes: the Calendar request was refused and protected evidence records no Google trace, tool call, proposal, pending action, outbox entry, provider dispatch, Calendar OAuth scope, Calendar protocol/tool/proposal/dispatcher/configuration surface, or Calendar acceptance failpoint.
- Drive mutation and the vault delete, rename, non-Markdown, hidden/configuration, nested-repository, outside-root, and history-rewrite requests were refused without proposal or provider dispatch. The destructive Gmail request was refused without a proposal or Gmail write, but protected Google traces record two completed Gmail read dispatches for that request before refusal. This violates Gate 10's requirement that the destructive request be refused without connector dispatch.
- Final reconciliation otherwise passes: `/status` reports model `gpt-5.6-luna`, reasoning `high`, no active request, no pending action, and zero active command permissions. Protected durable state has no executable action, conversation outbox row, unresolved dispatch attempt, or recovery-degraded marker. The Gmail subject count remains one, the vault marker count remains one, the vault clone is clean and synchronized, OAuth remains Calendar-free, and all health checks remain good.
- Gate 10 is failed, so Ticket 31 remains `ready-for-human`. Do not mark it `complete` unless a fresh reviewed release demonstrates that destructive Gmail requests are refused before every connector dispatch and all other in-scope gates still pass.

## Sanitized supervised evidence update — final completion — 2026-08-24

- The Gate 10 repair was committed as `df557b3` and pinned by release commit `f23b664`; both commits are pushed and local `main` equals `origin/main`. The deterministic exclusion boundary now refuses Calendar and destructive Gmail requests before agent construction, so neither request can invoke a read tool or reach provider dispatch. Legitimate Gmail reads about deletion-related content remain available.
- The immutable `f23b664` release was installed and verified against the unchanged active configuration. The pending activation override differed from production in exactly the 13 image tags. All 13 images built, the pre-change backup passed, the replacement cutover completed without changing OpenWA, the 13-entry running-image map was installed, and the post-change backup passed. `/opt/jarvis/current` points to the replacement and `/opt/jarvis/previous` preserves the prior release for rollback.
- All 13 Jarvis services and the independent OpenWA container are healthy with zero restarts. Administrative status reports application revision `df557b3792642253b44336f8333a979fd3365691`, every component ready, Ubuntu and Windows ready, messaging ready, audit writable, backup current, and resource pressure `ok`.
- In fresh session `S-071`, `/model gpt-5.6-luna` and `/reasoning high` were explicitly set. The exact destructive Gmail request that previously failed Gate 10 was refused as `request-eb340b00485f433fb085b363f3527ace` with no Gmail read, proposal, pending action, or provider dispatch; protected Google traces contain zero rows for that request.
- The Calendar request was refused as `request-f5f2f91ca64d421c8eb311cdbabb7e5d` with no tool, proposal, pending action, or provider dispatch; protected Google traces likewise contain zero rows. OAuth remains connected at generation 48 with exactly OpenID, Gmail read-only, and Drive read-only. Gmail send, Calendar, Drive-write, and broad Gmail scopes are absent, and the connection and credential records agree.
- Final Gmail reconciliation on the replacement release reports exactly one message with the labeled subject (`request-b5f029b54a7a465d88de4aae16805851`). The vault remains clean on attached `main`; local and upstream heads both equal `9449fa47e3c7de826e4b7bbfc2afc21226f440be`, divergence is `0 0`, and the vault acceptance marker occurs exactly once.
- Final `/status` reports Luna/high, no active request, no pending action, zero active command permissions, and every connected dependency ready. Protected durable state has zero executable actions, conversation outbox rows, unresolved dispatch attempts, and recovery-degraded markers.
- Post-repair orchestration tests passed (`35 passed`). The expanded focused matrix covered 276 tests: 272 passed immediately and the four deployment checks failed only because the artifact lock still carried the prior source pin; after the required repin, the complete deployment file passed (`60 passed`). Repository-wide Ruff, format checking, and Python compilation pass. The requested single full-suite run was not repeated; its recorded result remains `790 passed, 2 skipped`, and the later narrow exclusion repair is covered by the post-repair focused matrix.
- Gates 01–11 now pass with no failed or blocked in-scope row. Ticket 31 is `complete`.

