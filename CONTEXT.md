# Jarvis messaging

Jarvis currently provides the reliable messaging boundary for one dedicated
WhatsApp account. Intelligence and assistant behavior are a later concern and
are not part of the completed messaging context.

## Language

**Messaging gateway**:
The persistent boundary that receives and sends WhatsApp messages for the
dedicated account.
_Avoid_: Bot, AI assistant

**Messaging layer**:
Pairing, transport, persistence, readiness, and recovery for the messaging
gateway. It does not decide how a message should be answered.
_Avoid_: Assistant logic, agent behavior

**Dedicated account**:
The single WhatsApp account assigned to the messaging gateway.
_Avoid_: User account, sender

**Session**:
The gateway's named logical connection to the dedicated account. A session is
messaging-ready only when its state is `ready`.
_Avoid_: Container, HTTP health

**Messaging engine**:
The one active integration used by a session to connect to WhatsApp.
_Avoid_: Session, gateway

**Pairing state**:
Confidential retained authorization material that lets a session reconnect
without scanning a new QR code.
_Avoid_: API key, session name

**Assistant behavior**:
The future decision-making layer that determines whether and how Jarvis should
respond to an inbound message.
_Avoid_: Messaging layer, transport

**Reactive assistant behavior**:
Assistant behavior that starts only in response to an explicit inbound request
from the authorized operator. Milestone updates and completion messages belong
to that request; it does not monitor sources, run scheduled work, or start an
unrequested interaction.
_Avoid_: Automation, proactive monitoring, scheduled assistant behavior

**Authorized operator**:
The one person permitted to invoke assistant behavior, identified in V1 by an
allowlisted personal WhatsApp number. Messages from any other sender must not
invoke the assistant or disclose access to its connected services.
_Avoid_: Dedicated account, any WhatsApp sender

**Approval-gated action**:
A proposed change to a connected service that the assistant may execute only
after the authorized operator confirms that exact proposal. Approval for one
action does not authorize a different or later action.
_Avoid_: Autonomous action, standing permission, implicit approval

**Pending action**:
The one frozen approval-gated action awaiting a deterministic confirmation or
rejection message from the authorized operator. V1 permits at most one pending
action. It is bound to the exact working session and active request that created
it, pauses that request, and prevents unrelated work until it is confirmed,
rejected, cancelled, or expired. It becomes invalid when its session or request
ends, and it expires without execution after 10 minutes. A service restart
invalidates its executable confirmation state; the interrupted proposal remains
only in the audit record, and execution requires a fresh proposal and approval.
Its exact frozen target, operation, arguments, and preview persist only while the
action is pending; once terminal, that payload is removed immediately and only
redacted lifecycle metadata remains.
_Avoid_: Working session, queued action, approved action

**Active request**:
The single authorized request Jarvis is currently processing within a working
session. V1 does not queue or run a separate request in parallel with it. If the
service restarts, an active request becomes interrupted and cannot resume
execution without a new instruction from the authorized operator. Its persisted
record is limited to a stable ID, originating-message ID, lifecycle status and
phase, timestamps, cancellation or interruption reason, configuration snapshot,
tool and action correlation IDs, and outbound-attempt status.
_Avoid_: Pending action, working session, background automation

**Operational state**:
The lifecycle records and recovery metadata for working sessions, active
requests, pending actions, and orchestration checkpoints. After an item finishes,
expires, or is cancelled, its terminal operational state remains available for
30 days and is then removed automatically. This cleanup does not remove
conversation history or permanent audit records. A deterministic `/status` view
shows the current session and expiry, model configuration, request and pending-
action state, active command permissions, and connected-host or service readiness
without exposing cached content, credentials, raw tool payloads, or command output.
Terminal operational records are not directly browsable through Jarvis; after
they stop being live, only their redacted security events are operator-visible
through the audit view.
_Avoid_: Conversation history, durable assistant memory, audit record

**Jarvis state store**:
The local storage on the Ubuntu control host that is authoritative for
Jarvis-owned operational state, conversation history, durable memory, command
permissions, and audit references. Ordinary state is not application-encrypted;
only credential-class secrets that grant external access are encrypted
separately. Conversation history, durable memory, audit metadata, retrieval
indexes, and deleted conversations remain plaintext under strict filesystem
permissions. Credential-class secrets deliberately entered through the secret
store must not be copied into ordinary state, but an authorized-operator message
is retained verbatim even when its body contains a credential. OpenAI, Google,
OpenWA, and other external services are never authoritative stores for this
state. Deleted conversations reside in a separately permissioned local area
outside Jarvis-readable paths. Model-provider conversation persistence is
disabled; each model call receives only bounded current context and explicitly
selected or locally retrieved history. Provider identifiers are correlation
metadata and never the sole copy of session history or memory.
_Avoid_: OpenWA database, model conversation object, cloud state mirror

**Credential-class secret**:
Authorization material whose disclosure grants access to another system, such as
an OAuth refresh token, API key, webhook secret, private key, or messaging
authorization material. When deliberately provided through the credential path,
it is application-encrypted and stored separately from ordinary Jarvis state.
The verbatim conversation-history rule still applies when such material appears
inside an authorized-operator message.
_Avoid_: Conversation content, audit metadata, ordinary state field

**Administrative backup**:
A versioned local recovery copy stored outside Jarvis-readable paths and restored
only through a manual administrative process. It is not a cloud synchronization
target or an authoritative live state store, and it preserves the source data's
plaintext-or-encrypted classification. Every backup snapshot is retained
indefinitely and is never permanently removed automatically.
_Avoid_: Jarvis-accessible archive, cloud state mirror, live replica

**Text-only assistant interaction**:
V1 assistant behavior that accepts and returns text messages only. Media may
reach the messaging gateway, but the assistant does not download, interpret,
or respond to voice notes, images, documents, or other attachments.
_Avoid_: Multimodal assistant behavior, media processing

**Durable assistant memory**:
Information retained across conversations for assistant behavior. V1 retains
preferences only when the authorized operator explicitly asks it to remember
them; connected-service content is fetched from its source instead of copied
into durable assistant memory. Jarvis may infer a preference or fact from
conversation history for the current response, but that inference does not become
durable assistant memory without an explicit instruction to remember it. Every
saved memory is inspectable and remains until the authorized operator explicitly
forgets or replaces it. Replacing or forgetting a memory requires an exact-record
preview and confirmation. Forgetting removes its usable content while retaining
only content-free audit metadata about the deletion. If the authorized operator
explicitly asks Jarvis to remember a credential-class secret, the value is allowed
in durable assistant memory and remains plaintext under the ordinary-state rule.
A memory detected as containing credential-like material is excluded from
automatic model context and used only when the authorized operator explicitly
selects it. Each memory has a stable ID, exact saved content, creation and update
timestamps, an optional source-message pointer, and an explicit active, replaced,
or forgotten lifecycle state.
_Avoid_: Message history, connected-service mirror, implicit memory

**Conversation history**:
The durable, immutable record of every text message received from the authorized
operator and every text message Jarvis sends to that operator. Messages are never
edited in place; corrections append new messages, and removal occurs only through
the confirmed move-to-deleted workflow. Conversation history survives service
restarts and does not expire automatically; it remains until the authorized
operator explicitly deletes it. Deletion removes the selected conversation from
Jarvis's accessible history without erasing its retained content. Unauthorized,
group, and unsupported traffic contributes only minimal audit metadata, not
message bodies or media content.
One conversation is exactly one working session, from its first authorized
request until `/new` or inactivity expiry; browsing, export, and whole-
conversation deletion use that boundary.
Jarvis may automatically retrieve and use a bounded, relevant subset of
non-deleted history as context for a new request. Conversation history remains
distinct from durable assistant memory: retaining or retrieving a message does
not by itself turn its contents into a remembered preference or fact. Accessible
history is indexed and searched entirely on the Ubuntu host with local full-text
search and deterministic filters; V1 does not use semantic embeddings or send the
archive to an external retrieval provider. History is deterministically
searchable, inspectable, and exportable. A deletion must preview its exact scope
and receive confirmation before the selected history is moved to the deleted-
conversation area. A message detected as containing
credential-like material remains stored verbatim and deterministically searchable
but is excluded from automatic retrieval, retrieval indexes, and model context
unless the authorized operator explicitly selects it. When an answer uses a
message from an earlier conversation, Jarvis discloses that use and provides a
compact pointer to the source conversation and message timestamp.
_Avoid_: Working-session context, durable assistant memory, audit record

**Deleted conversation**:
Conversation history moved intact into a separate area marked as deleted. It is
retained rather than erased, but Jarvis assistant behavior cannot access, search,
summarize, restore, or use its content. Inspection and restoration require a
separate manual administrative process outside Jarvis. Moving history there
immediately removes every Jarvis-readable derivative, including index entries,
cached excerpts, retrieval pointers, and generated summaries; only content-free
tombstones remain accessible for message and audit references.
_Avoid_: Permanently erased conversation, accessible conversation history

**Terminal action**:
A command the assistant proposes to run on one execution host. An exact terminal
action is identified by its host, resolved executable or script path, complete
arguments, canonical working directory, and normalized compound-command
structure. Its inherited environment and the contents or metadata of files at
those paths are not part of its identity.
_Avoid_: Assistant response, tool suggestion

**Protected resource**:
A credential-bearing, security-sensitive, administratively isolated, explicitly
configured, or other-user-private resource that a terminal action may not read
automatically. An exact command permission may authorize its use unless a higher
terminal-policy rule prohibits the action or requires fresh approval.
_Avoid_: Every personal file, ordinary project file, hard-prohibited resource

**Hard-prohibited terminal action**:
A terminal action Jarvis refuses without offering an approval choice. It covers
broad or unresolved destructive targets, control or audit bypass, credential
extraction or transmission outside the credential path, covert persistence,
public control exposure, destructive Git history rewriting, and evidence
concealment. No command permission can authorize it.
_Avoid_: Approval-gated action, mandatory-fresh terminal action

**Mandatory-fresh terminal action**:
An allowed terminal action that must receive exact approval every time and can
never create or use a session or persistent command permission. In V1 this is
limited to system-level software installation, removal, or upgrade; changes to
Jarvis policy, audit, approval, worker, or orchestration components; materially
dynamic shell evaluation that cannot be frozen; and downloading then immediately
executing code.
_Avoid_: Hard-prohibited terminal action, every side-effecting command

**Auto-approval classification (V2)**:
A possible future advisory model classification for terminal actions. V1 does
not use Gemini or any other model to authorize commands; its policy is entirely
deterministic.
_Avoid_: V1 authorization, final authorization, security policy

**Command permission**:
A deterministic, narrowly structured rule that permits matching terminal
actions for one working session or until explicitly revoked. It cannot override
a hard prohibition or mandatory-approval rule. A service restart revokes every
session-scoped command permission but leaves a persistent permission intact
until it is explicitly revoked. Selecting a displayed `Allow for this session`
or `Allow every time` choice creates the exact permission immediately without a
second confirmation. Every active permission is
deterministically inspectable with its scope, creation time, and lifetime, and
the authorized operator may revoke it immediately. Permission state contains only
the exact normalized matching rule, lifetime, creation and authorization
provenance, and revocation state; it never stores command output or credential
values. Revocation immediately removes the usable rule while the audit retains
only redacted lifecycle metadata.
_Avoid_: Approval-gated action, wildcard command approval, model decision

**Knowledge vault**:
The authorized operator's private, Git-backed Obsidian repository used as a
connected source of personal knowledge. V1 may change it only through an exact
approval-gated commit and push from one dedicated clone on the Ubuntu control
host. The private Git remote, not another local Obsidian clone, is the
synchronization boundary. V1 searches it locally without an external content
index and may create or modify only Markdown notes in configured note paths.
_Avoid_: Durable assistant memory, Google Drive, unrestricted filesystem

**Vault write proposal**:
The one frozen knowledge-vault change awaiting approval. It includes the exact
remote base commit, canonical Markdown note paths, complete unified diff, and
commit metadata. Approval authorizes one commit and normal push of precisely
that proposal; any base, path, diff, or metadata change invalidates it.
_Avoid_: Command permission, autonomous note edit, general vault access

**Vault write conflict**:
A dirty dedicated clone, non-fast-forward state, or concurrent remote change
that prevents the exact approved vault write. Jarvis does not merge, rebase,
cherry-pick, force-push, rewrite history, or resolve the conflict autonomously;
the state requires manual resolution before another vault write.
_Avoid_: Transient network failure, approved automatic merge

**Connected-service cache**:
Request-scoped copies of content fetched from Gmail, Google Calendar, Google
Drive, the knowledge vault, or another connected source. Cached content is
cleared when the request ends or Jarvis restarts. Only encrypted credentials,
source identifiers, and non-content synchronization metadata persist; source
content is fetched again when needed.
_Avoid_: Connected-service mirror, durable assistant memory, conversation history

**Request working data**:
Raw tool results, terminal output, model working context, and other intermediate
content needed only by an active request. It is discarded when the request
completes and is not copied into operational state or the audit record. Text
Jarvis actually sends to the authorized operator is conversation history instead.
_Avoid_: Conversation history, terminal operational state, audit record

**Audit record**:
The append-only security history of Jarvis activity. It is retained indefinitely,
Jarvis cannot modify or delete it, and removal requires a separate manual
administrative process outside Jarvis. Jarvis may expose only a safe, redacted
inspection view to the authorized operator. It records bounded structured
metadata for identity, correlation, operation, target category, policy and
approval decisions, execution status, and outcome. It references conversation
messages by ID and excludes raw message bodies, connected-service content,
credentials, secrets, and unbounded tool or command output. Tool calls and
terminal actions appear only in a bounded form with sensitive arguments redacted.
The safe view is deterministically filterable by date, request, operation type,
target category, approval decision, and outcome, and only that filtered redacted
view may be exported through Jarvis. Append-only behavior is enforced through
storage and process permissions; the record is not cryptographically hash-chained
or independently tamper-evident. If a required event cannot be appended, Jarvis
blocks every side-effecting action but may continue safe read-only behavior with a
visible degraded-state warning until manual administration restores the audit.
Required events cover inbound admission and authorization; session and request
lifecycle changes; conversation-history and durable-memory access or mutation;
connected-service reads; tool proposals and outcomes; approvals and rejections;
command-permission changes; terminal executions; outbound-message attempts;
restarts; and security or degraded-state failures. Internal model reasoning is
diagnostic telemetry rather than a permanent audit event.
_Avoid_: Conversation history, model trace, editable activity log

**Diagnostic trace**:
Short-lived operational telemetry for model and agent runs. It may retain trace
and span identifiers, timing, model identity, token usage, span type, and
success-or-failure signals, but excludes model inputs and outputs, message
bodies, tool arguments, and tool results. Diagnostic traces expire automatically
after 30 days and are not the security audit record.
_Avoid_: Audit record, conversation history, prompt archive

**Execution host**:
A named computer on which Jarvis may evaluate and run terminal actions. V1 has
two execution hosts: the always-on Ubuntu laptop and the authorized operator's
Windows laptop when it is available.
_Avoid_: Messaging gateway, messaging engine, connected service

**Default execution host**:
The Ubuntu execution host used for host-neutral terminal actions when the
authorized operator does not name a host. An explicit host request takes
precedence over the default, and an unavailable requested host is never
silently substituted.
_Avoid_: Automatic failover host, any available computer

**Working session**:
The temporary conversational context that begins with an authorized request
and ends on `/new` or after the configured inactivity boundary, which defaults
to 60 minutes. The inactivity countdown is suspended while an active request is
genuinely processing and restarts when Jarvis becomes idle; a pending action's
separate 10-minute expiry is unaffected. Its lifecycle metadata, configuration,
conversation-history reference, and request status survive a service restart,
but interrupted model runs, tool calls, commands, and side effects do not resume
automatically.
Temporary model and reasoning choices belong to the working session; durable
assistant memory does not. `/new` atomically cancels any active request,
invalidates any pending action, revokes session-scoped command permissions, ends
the current working session, and starts a clean one without deleting any
separately durable state. Its persisted record is limited to a stable ID,
operator ID, lifecycle timestamps, model and reasoning settings, conversation
reference, current request and pending-action references, and session-scoped
permission IDs.
_Avoid_: WhatsApp chat, messaging session, durable assistant memory
