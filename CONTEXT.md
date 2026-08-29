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

**Personal assistant runtime**:
The single-operator process that receives admitted text from the messaging
gateway, routes deterministic commands, and runs ordinary requests through the
model-and-tool loop. It is a fresh replacement for the existing assistant
control plane rather than a compatible revision of it.
_Avoid_: Capability broker, enterprise control plane, messaging gateway

**Deterministic command**:
An exact slash-prefixed operator message handled by application code without
asking the model to interpret or execute it.
_Avoid_: Prompt, tool call, natural-language request

**Prepared tool**:
A named operation deliberately implemented and exposed to the model-and-tool
loop, such as reading the knowledge vault.
_Avoid_: Arbitrary capability, connector, terminal command

**Model-and-tool loop**:
The assistant cycle in which an ordinary operator message is sent to OpenAI,
prepared tool calls are executed, their results are returned to the model, and
the cycle ends with text for the operator.
_Avoid_: Deterministic command, capability broker

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
The one person permitted to invoke assistant behavior, identified by one
allowlisted personal WhatsApp number. Other senders, groups, self-authored
messages, and non-text messages do not enter the personal assistant runtime.
_Avoid_: Dedicated account, any WhatsApp sender

**Ingress admission**:
The durable disposition of one authenticated messaging-gateway event before any
assistant work begins. It is distinct from request acceptance and dependency
readiness; rejected or unsupported authenticated events may be acknowledged
without becoming conversation history or work.
_Avoid_: Request acceptance, webhook processing job, assistant execution

**Request acceptance**:
The creation of the single active request from an admitted authorized control
when Jarvis is idle. A second ordinary message during active work is retained in
conversation history but is refused rather than queued or joined to that request.
_Avoid_: Ingress admission, dispatch, queued request

**Dispatch readiness**:
The availability and identity check for the exact connector or execution host
immediately before a selected read or action. It never changes ingress
acknowledgement, creates a queue, or permits host failover.
_Avoid_: Ingress admission, request acceptance, general service health

**Approval-gated action**:
A proposed change to a connected service that the assistant may execute only
after the authorized operator confirms that exact proposal. Approval for one
action does not authorize a different or later action.
_Avoid_: Autonomous action, standing permission, implicit approval

**Pending action**:
The one exact mutating tool call or terminal command waiting for the authorized
operator to choose `1` (approve once), `2` (approve and save), or `9` (reject).
It pauses the active request without expiring; every other inbound message is
ignored except `/cancel`, and a runtime restart discards it.
_Avoid_: Working session, queued action, approved action

**Active request**:
The single authorized request currently being processed within a working
session. Another ordinary message is refused rather than queued, and a restart
discards the request instead of resuming it.
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
credential-class secrets that grant external access are stored separately in a
service-specific plaintext credential-file boundary. Static credentials are
root-owned; a connector that must rotate a credential owns only its own private
credential directory. Conversation history,
durable memory, audit metadata, retrieval indexes, and deleted conversations
remain plaintext under strict filesystem permissions. Credential-class secrets
deliberately entered through the credential path must not be copied into ordinary
state, but an authorized-operator message is retained verbatim even when its body
contains a credential. OpenAI, Google,
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
it is stored in a service-specific plaintext credential file outside Jarvis
state. Static files are root-owned and supplied only to their consuming service;
a connector that must rotate a credential may own a private `0700` directory and
`0600` file inaccessible to other Jarvis services. Credential files are excluded
from source control and routine backups. The verbatim conversation-history rule
still applies when such material appears inside an authorized-operator message.
_Avoid_: Conversation content, audit metadata, ordinary state field

**Administrative backup**:
A versioned local recovery copy stored outside Jarvis-readable paths and restored
only through a manual administrative process. It is not a cloud synchronization
target or an authoritative live state store, and it preserves the source data's
plaintext classification. Credential files are excluded and must be reprovisioned
separately, but full diagnostic traces and any credentials contained in their
payloads are included. Every backup snapshot is retained indefinitely and is
never permanently removed automatically.
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

**Terminal command**:
A shell command the personal assistant runtime proposes to run on the Ubuntu or
Windows execution host, in a stated working directory.
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
concealment. It also covers activating changes to Jarvis's trust-critical
components; Jarvis may prepare and test source changes, but deployment is a
manual administrative action outside Jarvis. No command permission can authorize
it.
_Avoid_: Approval-gated action, mandatory-fresh terminal action

**Trust-critical Jarvis component**:
A component or configuration whose activation can change message admission,
authorization, approval, audit, credential isolation, connector capabilities,
host identity, or control-surface exposure. This includes the installed Jarvis
runtime, capability broker, policy and approval parser, sender allowlist and
webhook verification, audit service, connectors, credential files and service
identities, worker CA and registration, service definitions, firewall rules, and
private-overlay grants. Jarvis may inspect, edit, and test development copies but
cannot activate them.
_Avoid_: Every project file, unrelated service, model prompt

**Mandatory-fresh terminal action**:
An allowed terminal action that must receive exact approval every time and can
never create or use a session or persistent command permission. In V1 this is
limited to system-level software installation, removal, or upgrade; materially
dynamic shell evaluation that cannot be frozen; and downloading then immediately
executing code. Activating a trust-critical Jarvis change is manual-only rather
than mandatory-fresh.
_Avoid_: Hard-prohibited terminal action, every side-effecting command

**Auto-approval classification (V2)**:
A possible future advisory model classification for terminal actions. V1 does
not use Gemini or any other model to authorize commands; its policy is entirely
deterministic.
_Avoid_: V1 authorization, final authorization, security policy

**Command permission**:
A TOML rule that automatically permits a terminal command when its execution
host and literal command prefix match. Choosing `2` saves the displayed rule;
the authorized operator may inspect, edit, or revoke it later.
_Avoid_: Read-only command rule, one-time approval, model decision

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
Request-scoped copies of content fetched from Gmail and Google
Drive, the knowledge vault, or another connected source. Cached content is
cleared when the request ends or Jarvis restarts. Only source identifiers and
non-content synchronization metadata persist in ordinary state; credentials live
separately in the plaintext credential-file boundary, and source content is
fetched again when needed, while full diagnostic traces retain any captured
payload indefinitely.
_Avoid_: Connected-service mirror, durable assistant memory, conversation history

**Request working data**:
Raw tool results, terminal output, model working context, and other intermediate
content needed only by an active request. It is discarded when the request
completes and is not copied into operational state or the audit record, but its
captured payload remains in the full diagnostic trace. Text Jarvis actually sends
to the authorized operator is conversation history as well.
_Avoid_: Conversation history, terminal operational state, audit record

**Orchestration agent**:
The model-driven planner that interprets an authorized request, selects a host
or connected-service operation, gathers bounded working data, and creates typed
proposals. It is never an authorization authority: its output cannot approve an
action, create a command permission, change policy, access connector credentials,
or dispatch a side effect without the capability broker.
_Avoid_: Capability broker, connector, authorization policy

**Capability broker**:
The deterministic authority that owns admission, policy evaluation, frozen
pending actions, approval matching, permission matching, replay prevention, and
side-effect dispatch. It accepts only newly admitted operator controls, invokes
only narrow connectors, and blocks side effects when required audit evidence
cannot be recorded. Model output and connected-source content cannot bypass or
change its decisions.
_Avoid_: Orchestration agent, model tool router, connector

**Connector**:
A narrow integration boundary that owns the credential and fixed operation set
for one external capability, such as Google, OpenWA outbound messaging, the
knowledge vault, or execution workers. A connector accepts only typed operations
from the capability broker; it does not expose raw credentials, arbitrary remote
endpoints, generic shell access, or operations outside the V1 action matrix.
_Avoid_: Orchestration agent tool, general HTTP client, capability broker

**Public OAuth callback**:
The sole public Jarvis HTTP endpoint, used only to complete an operator-initiated
Google OAuth authorization-code flow. It accepts the documented callback fields,
requires a short-lived single-use state match, hands the code to the Google
connector, and returns a content-free result. It exposes no message admission,
assistant control, state, approval, connector, audit, or execution operation.
_Avoid_: Public Jarvis API, webhook receiver, administrative interface

**Untrusted source content**:
Text or data retrieved from a connected service, the knowledge vault, terminal
output, quoted-message context, or another non-control source. It may inform an
answer but never conveys operator identity, approval, permission, policy, or tool
authority, even when it contains instruction-like language.
_Avoid_: Authorized operator control, approval, system policy

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
blocks every side effect including WhatsApp replies, failures, status responses,
and warnings; safe reads remain available only through local administration until
the audit is restored.
Required events cover inbound admission and authorization; session and request
lifecycle changes; conversation-history and durable-memory access or mutation;
connected-service reads; tool proposals and outcomes; approvals and rejections;
command-permission changes; terminal executions; outbound-message attempts;
restarts; and security or degraded-state failures. Internal model reasoning is
diagnostic telemetry rather than a permanent audit event.
_Avoid_: Conversation history, model trace, editable activity log

**Runtime trace**:
The rotating verbatim JSON Lines record of authorized messages, OpenAI request
and response payloads, prepared-tool calls and results, terminal activity,
approval choices, errors, and timing. It is operational evidence rather than an
authorization authority or searchable conversation archive.
_Avoid_: Audit record, hidden model reasoning, durable assistant memory

**Execution host**:
A named computer on which Jarvis may evaluate and run terminal actions. V1 has
two execution hosts: the always-on Ubuntu laptop and the authorized operator's
personal Windows laptop when it is available. The orchestration agent selects
the host from the natural-language request and known host purpose; the operator
does not need to use a routing command or fixed request prefix.
_Avoid_: Messaging gateway, messaging engine, connected service

**Default execution host**:
The Ubuntu execution host selected when the natural-language request does not
require or clearly refer to the authorized operator's personal Windows laptop.
Explicit operator intent and task dependencies take precedence over the default.
When the agent selects an unavailable host, it reports the decision and reason
and waits for further instruction; it never silently substitutes the other host.
_Avoid_: Automatic failover host, any available computer

**Working session**:
The in-memory conversational context that begins with an authorized request and
ends on `/new`, configured inactivity expiry, or runtime restart. It contains at
most one active request and one pending action and is never a durable conversation
archive. Reaching the configurable 100,000-token context limit measured with
`tiktoken` produces a deterministic notice and ends the session instead of
trimming or summarizing it.
_Avoid_: WhatsApp chat, messaging session, durable assistant memory
