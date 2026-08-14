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
  exec -T capability_broker uv run --no-project python -m \
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

Exercise unknown outcome only if the operator separately authorizes the exact
labeled test and the reviewer has a previously reviewed post-dispatch transport
interruption method. Require an unknown result, no automatic retry, and direct
Gmail reconciliation showing zero or one send. Record reconciliation before a
fresh request. If one send exists, compare the same complete material-field set
with the frozen proposal. If no method can prove post-dispatch interruption without new
authority, mark the row `blocked`; do not improvise with container kills,
firewall edits, or proxy replacement.

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
plus OpenID. Ask Jarvis to create one event in an operator-owned acceptance
calendar with summary `[JARVIS T31 ACCEPTANCE] ...`, a reviewed near-future
time, explicit timezone, no attendees, and notifications disabled. Compare the
complete frozen event, calendar target, action reference, and digest. Reply
exactly `yes` only after both humans agree. Verify exactly one event externally.
Open the event in Calendar, record the event ID returned by the create, and bind
that ID to reconciliation; compare an event ID with the frozen proposal only for
an update that supplied one. Compare the actual calendar, summary, description,
location, start/end, timezone, attendees, recurrence, visibility, and reminders
with the frozen proposal. Verify the exact notification choice from the
protected provider request/audit trace because it is not an event-resource/UI
field. Every material field must match without copying the trace into evidence.

First prove altered approval cannot dispatch: prepare a fresh labeled event,
reply `yes please`, require the pending action to remain blocked, then reply
exactly `reject` and verify no matching event exists. After the successful exact
approval above, send `yes` once as an old-approval replay check. It must not
redispatch the completed action. If treated as a new ordinary request, finish
or cancel it without approving a side effect. Reconcile Calendar directly,
require the labeled event count and fields to remain unchanged, and confirm no
fresh pending action or dispatch remains.

Exercise a Calendar unknown outcome only with separate authorization for the
exact labeled test and a previously reviewed post-dispatch interruption method.
Require an unknown result, no automatic retry, and direct Calendar
reconciliation showing zero or one mutation before any fresh request. If one
mutation exists, compare the same complete material-field set with the frozen
proposal. If no
method can prove the interruption without new authority, mark the row `blocked`;
do not improvise with container kills, firewall edits, or proxy replacement.

For stale-generation rejection, use this dedicated negative sequence instead
of section 3's ordinary reconnect procedure: begin with no pending action,
prepare one labeled event update, record its action reference and credential
generation, and leave only that intentional proposal pending. Disconnect and
reconnect Google without attempting or approving any other request. Confirm the
same proposal remains the intentional test, then reply `yes`; its old generation
must fail before dispatch and the event must remain unchanged. Confirm no pending
action remains. Do not create a replacement just to make this negative row
green. Calendar deletion is excluded; cleanup is a separate manual UI action or
separately approved reversible update.

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
| Calendar exact approval | One labeled event; actual material fields match the frozen proposal; one terminal outcome | |
| Calendar replay | No second dispatch or event | |
| Calendar unknown outcome | Separately approved, no retry, authoritative reconciliation | |
| Calendar stale generation | Old proposal rejected before dispatch; event unchanged | |
| Vault read | Synchronized deterministic read and bounded search | |
| Vault exact write | Exact diff, one commit, one normal push, clean matching heads | |
| Google exclusions | Destructive operations and Drive mutations refused without dispatch | |
| Vault exclusions | Delete/rename/non-Markdown/history operations refused without dispatch | |
| Audit and traces | Redacted audit complete; protected trace pointers retained | |
| Final reconciliation | No duplicate, unresolved unknown, stale action, dirty clone, or health regression | |
| Stop conditions | Explicit confirmation that none remains active | |

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
