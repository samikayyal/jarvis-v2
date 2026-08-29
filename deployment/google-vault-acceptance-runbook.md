# Ticket 31 real Google and knowledge-vault acceptance

This runbook closes implementation ticket 31 only after a human operator and a
second reviewer supervise the activated pinned release against the configured
Google identity and private knowledge vault. Automated or controlled-provider
results are useful preflight evidence, but are not production proof.

The run is split into read-only, rejected, and mutating gates. Do not continue
past a failed gate. Do not put account or external object identifiers, message
or note content, OAuth URLs or parameters, credentials, tokens, private remote
URLs, or private paths in Git or the sanitized evidence record.

## Human authority and stop conditions

- The operator sends the exact WhatsApp requests and exact whole-message
  approvals through the production control flow.
- The reviewer compares every frozen proposal with the intended external change,
  records sanitized evidence, and controls any failure injection.
- The operator must separately approve every Gmail, Calendar, or vault mutation
  after seeing its complete proposal. Agreement to run this worksheet is not
  approval for those mutations.

Stop immediately if the release differs from ticket 30 evidence; OpenWA is not
healthy and session-ready; audit is not writable; the Google subject or scopes
differ; a credential appears in output; a proposal changes a material field; a
side effect occurs before approval; a replay duplicates a side effect; or an
unknown outcome has not been reconciled. Also stop for a dirty, detached,
conflicted, or unexpectedly changed vault clone, or if Jarvis offers Drive
mutation, destructive Google behavior, vault deletion/rename, non-Markdown
mutation, force-push, rebase, history rewriting, or conflict resolution.

After a stop, preserve state and diagnostic evidence. Do not retry Gmail,
Calendar, or Git push work whose external outcome may be unknown. Reconcile in
the authoritative external system first, then require a fresh instruction.

## Evidence record

Create the record under the protected administrative evidence root used for
ticket 30, never in this repository. Record UTC times, operator/reviewer
initials, safely displayed request/action references, counts, booleans,
commit-object prefixes, protected-artifact hashes, and sanitized pointers only.
Screenshots must redact personal content and external identifiers.

```text
Ticket: 31
Run started (UTC):
Operator initials:
Reviewer initials:
Installed Jarvis commit:
Ticket 30 evidence pointer:
Protected evidence directory:
Stop condition encountered: yes/no
```

For every row record `started`, `finished`, `request_ref`, `action_ref` when
applicable, `sanitized_evidence_pointer`, and `pass/fail/blocked`.

## Acceptance repair rerun contract (Gates 03, 08-13, 15, and 18)

The following dependencies are part of the worksheet, not optional reviewer
notes. A controlled-provider unit test or a successful neighboring row does
not satisfy a real-system gate. This worksheet does not authorize activation,
deployment, active-configuration edits, service reloads, transport or
container interruption, firewall/proxy changes, retries, or direct-provider
workarounds.

| Gate | Rerun contract | Stop/defer rule |
| --- | --- | --- |
| Gate 03 | Prove the connected Google generation at the start of each bounded read; prove Calendar list/get grounding against the labeled calendar and event; prove Drive text export returns text through that same connected generation; and prove unsupported binary Drive export is refused without download. Gmail success does not cover Calendar or Drive. | Any Calendar grounding mismatch, stale-generation use, or literal `Google unavailable` result for the text export fails Gate 03. Do not run Calendar mutation gates until the Calendar read boundary is proven. |
| Gate 08 | Run Gmail unknown-outcome only with a separately reviewed, application-level post-dispatch failpoint owned by the Gmail connector. The protected review mechanism must bind it to the actual frozen, request-scoped Gmail action before exact approval, and its one-shot consumption must be durable across restart. | Gate 08 is Gmail-only and is not unblocked by Calendar evidence. If the mechanism cannot bind the exact pending action, preserve the target after a mismatch, or prove durable consumption/retirement, leave Gate 08 `blocked`; never improvise interruption or retry. |
| Gate 09 | After Gate 03 Calendar read evidence passes, send one prepare-only request through the deployed orchestration-to-Calendar action-prepare/write route for the preselected exact secondary calendar. It must return one complete frozen proposal before approval or any provider call, including the exact calendar target, operation, complete event fields, notification choice, request/action references, connected credential generation, and proposal digest. | A failure at the orchestration boundary before a complete proposal is returned is Gate 09 `fail`; record no pending action and no provider event, preserve the diagnostic evidence, and stop the Calendar mutation sequence. A controlled proposal or direct-provider construction is not a substitute. |
| Gate 10 | Only after Gate 09 passes, compare every field of the complete proposal with the exact secondary calendar and approve the intended labeled mutation using the whole-message exact approval. | If Gate 09 is `fail`, Gate 10 remains `deferred`; do not attempt approval, provider work, or a workaround. |
| Gate 11 | Only after Gate 10 has one completed exact action, replay the old exact approval and reconcile the exact secondary calendar plus durable Jarvis action state. | If Gate 10 has no completed action, Gate 11 remains `deferred`; never create a replacement side effect solely to exercise replay. |
| Gate 12 | Run Calendar unknown-outcome only after Gates 03 and 09 have passed and the reviewer has approved a Calendar-specific, application-level post-dispatch failpoint bound through the protected review mechanism to the actual frozen request-scoped Calendar action before approval. Its one-shot consumption and retirement must be durable across restart, with a provider-first and durable-state reconciliation plan. | Missing the Calendar proposal prerequisite, exact action binding, durable consumption, or reviewed retirement leaves Gate 12 `blocked`; do not substitute Gmail's failpoint or manufacture interruption, retry, or a direct-provider workaround. |
| Gate 13 | Run the dedicated stale-generation sequence only after the Calendar read and proposal boundaries pass, with one intentional old-generation proposal for the exact secondary calendar and no replacement request. | If Gate 03 or Gate 09 is not a real-system `pass`, Gate 13 remains `deferred`; do not claim stale-generation coverage from controlled tests alone. |
| Gate 15 | For the exact Markdown write, capture one terminal Jarvis acknowledgement tied to the action reference that explicitly says either success (including commit/push evidence) or `outcome-unknown` (including the manual-recovery instruction). Git heads alone are not an acknowledgement. | A missing, ambiguous, or undifferentiated terminal acknowledgement leaves Gate 15 `blocked`, even if the Markdown change appears committed and pushed. |
| Gate 18 | Aggregate the repaired rows only after Gates 03, 08, 09, 10, 11, 12, 13, and 15 each have a real-system `pass`, final reconciliation is clean, and no unknown, stale action, pending action, dirty clone, or unresolved review target remains. | Any `fail`, `blocked`, or `deferred` input keeps Gate 18 `fail`; do not mark the ticket complete or report live acceptance. |

### Reviewed failpoint binding, durable consumption, reconciliation, and retirement

The normal production configuration has no `acceptance_failpoint` section and
therefore has no fault injection. The optional active-configuration section is
root-owned and read-only inside the deployed services and is only the reviewed
mechanism's bounded target description; it is not an operator-facing chat
command and this worksheet does not authorize writing it, activating it,
deploying it, or reloading a service. A separately authorized
human administrator must perform those trust-critical steps outside this
worksheet.

```toml
[acceptance_failpoint]
enabled = true
service = "gmail"             # `gmail` or `calendar`
operation = "gmail_send"      # service-specific reviewed operation
action_id = ""                 # bound to the frozen action by the reviewed mechanism
review_id = "ticket31-gmail-unknown"
```

For a separately reviewed Calendar unknown-outcome row, the same section uses
the Calendar operation and leaves `action_id` empty until the protected
mechanism binds the exact frozen request-scoped action:

```toml
enabled = true
service = "calendar"
operation = "insert"          # or the reviewed `update`/`patch` operation
action_id = ""
review_id = "ticket31-calendar-unknown"
```

Only one exact service/operation review target is armed at a time; these
references are not credentials or external object identifiers.

The five fields are required exactly as shown: `enabled`, `service`,
`operation`, `action_id`, and `review_id`. The operation must be one of
`gmail_send`, `gmail_reply`, `insert`, `update`, or `patch` and must belong to
the selected service; `action_id` and `review_id` are exact bounded identifiers,
not wildcards or prefixes. An enabled active configuration must leave
`action_id` empty: the protected mechanism binds it to the connector-owned
frozen action after proposal preparation. Never fill it with a guessed,
precomputed, label-derived, or copied action ID. Use a separately reviewed
Calendar target for Gate 12, also with an empty `action_id` before binding.

For each unknown-outcome row, use this order:

1. While Jarvis is idle with no pending action, a separately authorized
   administrator must have loaded the reviewed active configuration through the
   protected deployment mechanism. It has `enabled = true`, the exact reviewed
   service and operation, an empty `action_id`, and the non-secret `review_id`.
   The configuration is startup-loaded; this worksheet does not authorize its
   activation, deployment, edit, or reload. Do not arm or reload after a
   proposal has been prepared: a service reload invalidates the pending action
   and requires the row to be aborted and restarted from idle.
2. With that empty-action arm already active, prepare the real request through
   Jarvis and record the exact request reference, displayed pending action
   reference, operation, proposal digest, connected generation, and complete
   frozen proposal. For Calendar, verify the exact preselected
   secondary-calendar target. The first matching connector-owned frozen
   proposal binds the review to its actual request-scoped action through the
   protected mechanism; no model or chat input supplies or retargets the
   `action_id`.
3. Before the operator sends `yes`, the reviewer compares the displayed
   pending action and digest with the protected bound marker, review reference,
   operation, generation, and complete proposal. Verify the durable bound
   marker names the same action and survives a read of the protected state. If
   the action, digest, target, generation, or marker does not match, do not
   approve: deterministically reply `reject` when the pending action is still
   rejectable, otherwise abort and preserve state. The gate is `blocked` and no
   provider call may occur.
4. Do not reload, replace, or otherwise change the active configuration between
   this bound-proposal check and exact approval. Send the exact approval only
   after both humans have compared the proposal. The connector must call the
   failpoint after the provider has returned and before Jarvis emits the
   terminal acknowledgement. The result must be one durable `unknown` action
   outcome, with no automatic retry and no second provider call.
5. Reconcile provider state first, using read-only list/get/search in the
   authoritative Gmail or Calendar system. Record zero or one labeled side
   effect. If one exists, compare every material field with the frozen
   proposal. Direct provider mutation, a second send/create/update, or a
   provider-side workaround is prohibited.
6. Reconcile Jarvis durable state second. Verify the action is terminal
   `unknown`, the active request and pending action are absent, no retry/outbox
   entry remains executable, and the audit/diagnostic trace retains only
   sanitized action, generation, review, and reconciliation pointers.
7. Prove the failpoint's consumed/retired disposition survives a controlled
   service or control-plane restart. An in-memory one-shot flag, a test-process
   result, or a provider count alone is not proof. After restart, the same
   action/review target must not fire again and must not permit a replay.
8. Only after both reconciliations and the durable restart check, a separately
   authorized administrator retires the target by restoring the normal absent
   section (preferred) or setting `enabled = false` with all four target fields
   empty. Verify the protected mechanism reports disabled/retired, no pending
   action or unresolved unknown remains, and the retired target cannot fire
   after another restart. Never reuse an action ID; obtain a new review for a
   new acceptance run.

If any provider or Jarvis durable state is uncertain, preserve state and stop.
Do not restart to manufacture an outcome, do not retry, and do not manipulate a
transport, container, firewall, or proxy. A missing durable consumption marker
or missing protected retirement evidence leaves the relevant gate `blocked`.

## 1. Pin and readiness preflight

Run on the activated Ubuntu host from the exact installed release directory.
Use the same reviewed paths as ticket 30. Do not use `HOME`, `~`, command
substitution, or a glob.

```bash
export JARVIS_RELEASE_DIR=/opt/jarvis/releases/<installed-commit>
export JARVIS_ACTIVE_OVERRIDE=/etc/jarvis/activation.compose.yaml
cd "$JARVIS_RELEASE_DIR"
git rev-parse --verify HEAD
sha256sum deployment/artifacts.lock.json "$JARVIS_ACTIVE_OVERRIDE"
docker compose --file deployment/compose.yaml \
  --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation config --quiet
docker compose --file deployment/compose.yaml \
  --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation ps
docker compose --file deployment/compose.yaml \
  --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation \
  exec --interactive=false -T capability_broker uv run --no-project python -m \
  jarvis_control_plane.service_runtime admin-status
```

Compare the commit, artifact lock, override hash, and image map with protected
ticket 30 evidence. Require all services healthy, messaging ready, audit
writable, no resource pressure, and an idle session. Use `/status` if needed;
record no conversation content.

Check credential metadata only. Never run `cat`, `head`, `tail`, `strings`, an
editor, or an archive against these directories.

```bash
sudo stat --format='%n owner=%u group=%g mode=%a type=%F' \
  /etc/jarvis/credentials/google \
  /etc/jarvis/credentials/google/credentials.json \
  /etc/jarvis/credentials/vault \
  /etc/jarvis/credentials/vault/ssh_config \
  /etc/jarvis/credentials/vault/known_hosts
```

Require reviewed ownership and deployment-contract modes. Reuse ticket 30's
exposure checks: only the exact OAuth callback may be public. Do not publish a
port, broaden a firewall, or attach a new network.

Run the automated boundary suite from the pinned installed source:

```bash
uv run pytest -q \
  tests/test_ticket16_google_oauth.py \
  tests/test_ticket17_google_reads.py \
  tests/test_ticket18_gmail_writes.py \
  tests/test_ticket19_calendar_actions.py \
  tests/test_ticket23_knowledge_vault.py \
  tests/test_ticket24_knowledge_vault.py \
  tests/test_ticket27_vault_repository.py
```

Passing controlled tests is necessary but satisfies no real-system row.

## 2. Baseline Google consent and fixed scopes

If the connection is not already the reviewed baseline identity, start consent
through the authenticated broker with a non-identifying operation ID:

```bash
docker compose --file deployment/compose.yaml \
  --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation \
  run --rm capability_broker \
  google-authorize --operation-id ticket31-baseline-01
```

Open the single-use URL only in the reviewer's controlled browser. Require
exactly OpenID identity, Gmail read-only, Calendar list read-only, Calendar
events read-only, and Drive read-only. Gmail send and Calendar write must be
absent. The callback must bind the returned OpenID subject to the configured
subject; an account mismatch must fail without replacing a valid grant. Record
scope names and the identity-match boolean, never the URL, account, code, token,
or callback query.

## 3. Real bounded reads and reconnection

Start each request only when `/status` is idle. Use pre-created, conspicuously
labeled data containing no secrets or third-party personal data.

1. Request at most three Gmail messages matching the acceptance label, then one
   bounded textual message read.
2. Request the acceptance calendar and at most three labeled events in a narrow
   time range, then one bounded event read.
3. Request one labeled text or supported Workspace Drive document by list/get or
   export. Confirm non-text binary media is not downloaded or interpreted.
4. Externally confirm no email, event, or Drive object changed.

For the Calendar result, record the bounded exact calendar target, the labeled
event correlation, and the generation used for the provider call. A plausible
event list without target/event grounding is not a passing Calendar read. Before
any Calendar mutation, the reviewer must preselect one exact operator-owned
secondary calendar and carry its sanitized reference into the prepare-only
request; a generic "acceptance calendar" description is insufficient. For the
Drive result, the labeled text export must complete while the same connected
generation is current; a sanitized `Google unavailable` result is a failed
text-export row, not an authentication diagnosis. Keep the binary refusal as a
separate negative check and verify that no binary bytes were downloaded.

Record result counts, bounds/staleness disclosures, request references, and
redacted audit pointers, not returned content.

Then exercise revocation and reconnection:

1. Confirm there is no pending action.
2. Disconnect through the broker command below.
3. Repeat one read; require a sanitized unavailable/authentication result, no
   side effect, and no stale credential use.
4. Reauthorize with a new operation ID and repeat the same read successfully.

```bash
docker compose --file deployment/compose.yaml \
  --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation \
  run --rm capability_broker google-disconnect
docker compose --file deployment/compose.yaml \
  --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation \
  run --rm capability_broker \
  google-authorize --operation-id ticket31-reconnect-01
```

Record the disconnect, failed read, new bound generation, successful fresh
read, and audit pointers. Google-console revocation alone is not proof that
Jarvis removed its local grant.

## 4. Gmail exact approval, alteration, replay, and uncertainty

Add only the named incremental Gmail send capability:

```bash
docker compose --file deployment/compose.yaml \
  --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation \
  run --rm capability_broker \
  google-authorize --operation-id ticket31-gmail-send-01 --access gmail-send
```

The consent must retain baseline scopes and add only Gmail send plus OpenID.
Use an operator-owned destination, a subject beginning
`[JARVIS T31 ACCEPTANCE UTC ...]`, and a body that identifies reversible
acceptance data without anyone else's data.

First prove altered approval cannot dispatch:

1. Ask Jarvis to prepare the labeled email and compare complete To, Cc, Bcc,
   subject, body, MIME type, attachments/other send fields, thread binding,
   action reference, and digest.
2. Reply `yes please`. Whole-message grammar must not dispatch; the pending
   action remains blocked.
3. Reply exactly `reject`. Verify no matching sent message exists.

Create a fresh proposal with a new UTC label. Compare every field and reply
exactly `yes` only after both humans agree. Require one sent message and one
terminal action outcome. Open the sent message in Gmail and compare its actual
To, Cc, Bcc, subject, body, MIME type, attachments/other send fields, and thread
binding with the frozen proposal; every material field must match.

After completion, send `yes` once as an old-approval replay check. It must not
redispatch the completed action. If treated as a new ordinary request, finish or
cancel it without approving a side effect. The sent count must remain one. An
identical signed webhook/message-ID replay may use only ticket 30's reviewed
replay mechanism; it must retain its original disposition. Never expose the
signing secret or create an ad hoc replay tool.

Exercise Gate 08 (Gmail unknown outcome) only if the operator separately
authorizes the exact labeled test and the reviewer has a previously reviewed
application-level post-dispatch failpoint owned by the Gmail connector. Bind it
through the protected reviewed mechanism to the actual frozen request-scoped
action before approval, as described above. It is armed for one exact
operation/action and runs after the provider returns but before terminal
acknowledgement; it does not interrupt transport, a firewall, a proxy, or a
container. The review record must identify the Gmail operation, the boundary
after provider dispatch and before terminal acknowledgement, the failpoint
owner, and the authoritative reconciliation steps. Require an unknown result,
no automatic retry, provider-first Gmail reconciliation showing zero or one
send, and then durable Jarvis reconciliation before a fresh request. If one
send exists, compare the same complete material-field set with the frozen
proposal. This is a Gmail-only gate and does not wait on or repair a Calendar
prerequisite. If no reviewed failpoint can prove exact action binding, durable
consumption, or protected retirement without new authority, mark Gate 08
`blocked`; do not improvise with a transport interruption, container kill,
firewall edit, proxy replacement, or a retry.

A Gmail reply may additionally use an operator-owned acceptance thread. Verify
frozen source message/thread/recipient/header binding and the returned thread.
It does not replace the required new-send row.

## 5. Calendar exact approval, alteration, replay, uncertainty, and stale generation

Add only the named incremental Calendar write capability:

```bash
docker compose --file deployment/compose.yaml \
  --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation \
  run --rm capability_broker \
  google-authorize --operation-id ticket31-calendar-write-01 \
  --access calendar-write
```

The consent must retain prior reviewed scopes and add only Calendar events write
plus OpenID. Before the request, the reviewer selects one exact operator-owned
secondary calendar and records only a sanitized reference to it. Do not use the
primary calendar, infer a target from model output, or accept a proposal whose
target is merely described as "an acceptance calendar". Ask Jarvis to prepare
one labeled event for that exact secondary calendar with summary
`[JARVIS T31 ACCEPTANCE] ...`, a reviewed near-future time, explicit timezone,
no attendees, and notifications disabled. The prepare-only response from the
deployed orchestration-to-Calendar action-prepare/write route must be one
complete frozen proposal before approval or a provider call. It must include the
exact `calendar_id`, operation, complete event payload, notification choice,
request/action references, connected credential generation, and digest. Compare
every field with the preselected secondary calendar. Reply exactly `yes` only
after both humans agree. Verify exactly one event externally in that same
secondary calendar.
Open the event in Calendar, record the event ID returned by the create, and bind
that ID to reconciliation; compare an event ID with the frozen proposal only for
an update that supplied one. Compare the actual calendar, summary, description,
location, start/end, timezone, attendees, recurrence, visibility, and reminders
with the frozen proposal. Verify the exact notification choice from the
protected provider request/audit trace because it is not an event-resource/UI
field. Every material field must match without copying the trace into evidence.

First prove Gate 09's altered approval cannot dispatch: prepare a fresh labeled
event for the same exact secondary calendar, reply `yes please`, require the
pending action to remain blocked, then reply exactly `reject` and verify no
matching event exists. If the prepare-only request fails before producing the
complete proposal, record Gate 09 `fail`, preserve the no-pending/no-event
state, and stop; do not attempt exact approval, replay, or an alternate
Calendar/provider route. After Gate 09 passes, the exact approval above is Gate
10. After the successful exact approval, send `yes` once as the old-approval
replay check for Gate 11. It must not redispatch the completed action. If treated
as a new ordinary request, finish
or cancel it without approving a side effect. Reconcile Calendar directly,
require the labeled event count and fields in the exact secondary calendar to
remain unchanged, and confirm no fresh pending action or dispatch remains.

Exercise Gate 12 (Calendar unknown outcome) only after Gates 03 and 09 pass,
with separate authorization for the exact labeled test and a previously
reviewed Calendar-specific application-level post-dispatch failpoint owned by
the Calendar connector. Bind it through the protected reviewed mechanism to
the actual frozen request-scoped action before approval, as described above. It
is armed for one exact operation/action and runs after the provider returns but
before terminal acknowledgement; it does not interrupt transport, a firewall,
a proxy, or a container. The review record must identify the Calendar operation,
the boundary after provider dispatch and before terminal acknowledgement, the
failpoint owner, and the authoritative reconciliation steps. Require an
unknown result, no automatic retry, provider-first reconciliation showing zero
or one mutation in the exact secondary calendar, and then durable Jarvis
reconciliation before any fresh request. If one mutation exists, compare the
same complete material-field set
with the frozen proposal. If either prerequisite, exact action binding, durable
consumption, or reviewed retirement is missing, mark Gate 12 `blocked`; do not
substitute Gmail's failpoint or improvise with a transport interruption,
container kill, firewall edit, proxy replacement, or a retry.

For stale-generation rejection, use this dedicated negative sequence instead
of section 3's ordinary reconnect procedure: begin with no pending action and
passing Gates 03 and 09. Gate 03's connector-owned Calendar read supplies the
credential generation; prepare one labeled event update for the exact
secondary calendar through the deployed route, and require the frozen proposal
to carry that same read generation. A model-supplied or invented generation is
not valid evidence. Record its action reference and generation, and leave only
that intentional proposal pending. Disconnect and reconnect Google only after
the proposal is pending, without attempting or approving any other request.
After reconnect, do not make a fresh Calendar read or prepare a new proposal:
that would exercise a new generation rather than prove rejection of the stale
pending proposal. Confirm the original complete proposal remains the
intentional test, including its exact secondary-calendar target, then reply
`yes`; its old generation must fail before dispatch and the event must remain
unchanged. Confirm no pending action remains. Do not create a replacement just
to make this Gate 13 negative row green. If the Calendar proposal is
unavailable, Gate 13 is `deferred`. Calendar deletion is excluded; cleanup is
a separate manual UI action or separately approved reversible update.

## 6. Deterministic vault read and exact normal push

Check the dedicated clone as UID 10006 without displaying remote URL, content,
or author identity. Run no Git repair command.

```bash
docker compose --file deployment/compose.yaml \
  --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation \
  exec --user 10006 knowledge_vault_connector \
  git -C /var/lib/jarvis/vault status --short --branch
docker compose --file deployment/compose.yaml \
  --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation \
  exec --user 10006 knowledge_vault_connector \
  git -C /var/lib/jarvis/vault rev-parse --verify HEAD
```

Require a clean attached branch at the synchronized base. Ask Jarvis for one
deterministic path read and one bounded search of a pre-created acceptance note.
Record counts and the synchronized/stale disclosure, not content.

Ask Jarvis to change only a `Last acceptance run` UTC marker in a pre-existing
ordinary Markdown note inside a configured directory. The frozen proposal must
show the exact local/remote base, one canonical Markdown path, complete unified
diff, configured commit identity and fixed subject, and that approval performs
one commit and one normal push. Reply exactly `yes` only after both humans
compare every field.

Verify matching local and remote heads, a clean clone, exactly one approved path,
and a committed diff equal to the frozen diff. Verify the actual local and remote
commit subject is the fixed `jarvis:` subject and its author identity equals the
configured identity from the frozen proposal. Record only commit prefixes,
subject conformance, identity match, and hashes of protected evidence, not the
remote URL, full note, author name, or author address.

Require Jarvis to send one terminal completion acknowledgement for the same
action reference after reconciliation. The acknowledgement must distinguish a
successful commit-and-push from `outcome-unknown` requiring manual recovery;
do not infer success from matching Git heads or from a change that merely
appears to have committed. A missing or ambiguous acknowledgement is a Gate 15
failure/block, and the run must stop before any retry or fresh vault request.

Do not run a manual `git push` to rescue a failed/unknown Jarvis push. Reconcile
against the private remote before a fresh instruction. Exercise dirty clone,
changed base, conflict, and non-fast-forward only in an isolated non-production
clone or as a pre-dispatch condition; never damage production to manufacture a
failure.

## 7. Excluded-capability rejection

Run as separate requests with no pending action. They must end in deterministic
refusal or closed-schema failure and must never create a proposal or connector
dispatch:

- delete Gmail mail or a Calendar event;
- modify/upload a Drive file;
- delete/rename a vault note;
- write non-Markdown, hidden, plugin, configuration, attachment,
  nested-repository, or outside-configured-directory paths;
- force-push, rebase, rewrite history, or resolve a vault conflict.

Use fictional or labeled acceptance targets, never a real destructive target.
Confirm no external change and record bounded refusal, no pending action, no
dispatch, and the audit pointer.

## 8. Final reconciliation and worksheet

Reconcile every label and commit externally; no unknown may remain. Confirm the
current Google generation, no stale action, clean synchronized vault, idle
`/status`, healthy administrative status, writable audit, no resource pressure,
and complete redacted audit evidence. Reference protected diagnostic traces;
never copy them into the worksheet.

| Gate | Required evidence | Outcome |
| --- | --- | --- |
| Release/readiness | Ticket 30 pin match, healthy services, ready messaging, writable audit, idle state | |
| Credential metadata | Exact ownership/modes; no values displayed | |
| Exposure | OAuth callback only; no new network authority | |
| Baseline scopes | Exact fixed read scopes and configured-subject match | |
| Gmail reads | Real bounded list/get, no side effect | |
| Calendar reads | Real bounded list/get, no side effect | |
| Drive reads | Real bounded list/get/export and binary refusal, no mutation | |
| Disconnect/reconnect | Failed stale read, new bound generation, successful fresh read | |
| Gmail altered approval | No dispatch and no sent message | |
| Gmail exact approval | One labeled send; actual material fields match the frozen proposal; one terminal outcome | |
| Gmail replay | No second dispatch or message | |
| Gmail unknown outcome | Separately approved, no retry, authoritative reconciliation | |
| Calendar altered approval | No dispatch and no matching event | |
| Calendar exact approval | One labeled event in the exact secondary calendar; actual material fields match the frozen proposal; one terminal outcome | |
| Calendar replay | No second dispatch or event | |
| Calendar unknown outcome | Separately approved, no retry, authoritative reconciliation | |
| Calendar stale generation | Old proposal rejected before dispatch; event unchanged | |
| Vault read | Synchronized deterministic read and bounded search | |
| Vault exact write | Exact diff, one commit, one normal push, clean matching heads | |
| Google exclusions | Destructive operations and Drive mutations refused without dispatch | |
| Vault exclusions | Delete/rename/non-Markdown/history operations refused without dispatch | |
| Audit and traces | Redacted audit complete; protected trace pointers retained | |
| Final reconciliation | No duplicate, unresolved unknown, stale action, dirty clone, unretired failpoint target, or health regression | |
| Stop conditions | Explicit confirmation that none remains active | |

The worksheet is the source for Gate 18 aggregation: copy each repaired gate's
actual outcome into the table only after its prerequisites and evidence are
complete. Gate 18 is `pass` only when Gates 03, 08, 09, 10, 11, 12, 13, and 15
all have real-system `pass` outcomes, provider-first and durable Jarvis
reconciliation are complete, and the failpoint target is durably retired. Any
`fail`, `blocked`, or `deferred` input keeps Gate 18 `fail`. A controlled-
provider result may explain a preflight failure or validate a harness, but
cannot turn a live row green.

## Closing ticket 31

Only after every required row passes and no unknown remains:

1. Check all three ticket 31 acceptance boxes.
2. Append a `## Comments` entry with sanitized date, installed commit, protected
   evidence pointer, reads/scopes summary, mutation counts, reconnect/replay/
   unknown results, vault commit prefix, exclusions, and final health result.
3. Set `Status: complete` and commit only that sanitized ticket update.

Never commit evidence, screenshots, OAuth material, identifiers, or test data.
If any row fails or is blocked, leave the ticket `ready-for-human` and make no
claim of completion.
