Type: grilling
Status: resolved
Blocked by: 01, 03

## Question

What exact state belongs to a working session, active request, pending action, durable assistant memory, connected-service cache, command permission, and audit record; where is each stored; how long is it retained; how is it inspected or deleted; and which message, tool, and command content must be redacted or excluded?

## Answer

Jarvis owns its application state on the Ubuntu control host. V1 uses one
unencrypted local SQLite database, with transactional writes and WAL mode, for
operational state, accessible conversation history, durable assistant memory,
command permissions, and local full-text retrieval indexes. Credential-class
secrets deliberately entered through the credential path live in a separate
service-specific plaintext credential-file boundary. Static files are root-owned;
a connector that must rotate a credential owns only its private credential
directory. Audit data is
isolated in a plaintext append-only store,
and deleted conversations are moved intact into a separately permissioned
plaintext directory outside Jarvis-readable paths. OpenAI, Google, OpenWA, and
other external systems are never authoritative Jarvis state stores.

Automatic versioned backups cover the SQLite database, audit store, deleted-
conversation directory, and complete diagnostic-trace store. Credential files
are excluded and must be reprovisioned separately, but trace payloads and backups
retain any credentials captured during model, tool, Codex, connector, or worker
runs. Backups remain local, outside Jarvis-readable paths, require manual
administration to restore, preserve the source's highest plaintext sensitivity,
and are retained indefinitely; no backup snapshot is permanently removed
automatically.

### State and lifecycle

| State | Exact durable content | Lifetime and restart behavior | Inspection and removal |
| --- | --- | --- | --- |
| Working session | Stable ID, operator ID, start/last-activity/expiry timestamps, model and reasoning settings, conversation reference, active-request and pending-action references, and session-permission IDs. | Defaults to 60 minutes of inactivity. Genuine active processing suspends the inactivity countdown; it restarts when Jarvis becomes idle. Lifecycle state survives restart, but interrupted work does not resume. `/new` atomically cancels the active request, invalidates the pending action, revokes session permissions, ends the session, and starts a clean one without deleting durable data. | Current safe state is visible through deterministic `/status`. Terminal operational state remains internally for 30 days, then is removed. There is no `/requests` history browser. |
| Active request | Stable ID, originating-message ID, lifecycle status and phase, timestamps, cancellation/interruption reason, configuration snapshot, tool/action correlation IDs, and outbound-attempt status. | One active request at a time. Restart marks it interrupted and requires a new operator instruction; no model run, tool call, command, or side effect resumes automatically. Terminal metadata remains 30 days. | Live state is visible through `/status`; older operator-visible facts come from the redacted audit. Raw working content is not part of this record. |
| Pending action | Exact frozen target, operation, arguments, preview, expiry, and ownership by one session/request while actionable. | One at a time, pauses its owning request, blocks unrelated work, and expires after 10 minutes. It becomes invalid if the request/session ends or the service restarts. Once confirmed, rejected, cancelled, expired, or invalidated, the exact payload is removed immediately and execution requires a fresh proposal after restart. | The safe summary and expiry appear in `/status`. Confirmation is valid only for the exact frozen proposal. Terminal operational and audit records retain redacted lifecycle metadata, not the proposal payload. |
| Conversation history | Every authorized-operator inbound text and every Jarvis outbound text, verbatim and immutable, with stable message/session relationships. One conversation is one working session. | Survives restarts and has no automatic expiry. Corrections append new messages; messages are never edited in place. | Deterministic local search, inspection, and export are allowed. A deletion previews an exact message, date-range, or conversation scope and requires confirmation, then moves the immutable content to the deleted-conversation directory. Inspection or restoration of deleted content is manual and outside Jarvis. |
| Durable assistant memory | Stable ID, exact explicitly saved content, creation/update timestamps, optional source-message pointer, and active/replaced/forgotten status. | Persists indefinitely until exact confirmed replacement or forgetting. History may inform one response, but no fact or preference becomes durable memory without an explicit remember instruction. | Every active memory is listable. Replacement/forgetting previews the exact record and requires confirmation. Forgetting removes usable content while leaving only content-free audit metadata. |
| Connected-service cache and request working data | Request-scoped connected-service content, retrieved excerpts, raw tool results, terminal output, model working context, and other intermediates. Durable connected-service state is limited to separately held plaintext credential files, source identifiers, and non-content synchronization metadata. | Cleared from working storage when the request ends or the service restarts. Complete captured payloads remain indefinitely in diagnostic traces and backups. Text deliberately sent to the operator becomes conversation history as well. | Working data is not independently browsable or exportable. It is never copied into operational state or audit content; trace inspection is manual administration only. |
| Command permission | Exact normalized matching rule, lifetime, creation/authorization provenance, and revocation state; never command output or credential values. | Selecting the displayed session or persistent option creates that permission immediately. Session-scoped permissions end with the session and are revoked on restart; persistent permissions survive restart until explicitly revoked. | A deterministic command lists every active permission with scope, creation time, and lifetime and revokes any permission immediately. Revocation removes the usable rule; only redacted lifecycle metadata remains in audit. The terminal-policy ticket owns rule eligibility and matching semantics. |
| Audit record | Bounded metadata for identity and correlation, operation and target category, policy and approval decisions, execution status, and outcome. It references messages by ID. | Append-only and retained indefinitely. Jarvis cannot modify or delete it; removal is manual administration outside Jarvis. Append-only behavior relies on storage/process permissions and is not cryptographically hash-chained or independently tamper-evident. | A deterministic read-only view filters by date, request, operation type, target category, approval decision, and outcome and exports only that redacted view. If a required event cannot be appended, every WhatsApp response and side effect fails closed; safe reads remain local-administration-only. |
| Diagnostic trace | Complete trace and span metadata plus every captured prompt, message, model input/output, retrieved excerpt, tool and Codex argument/result, connector result, terminal input/output, error, and credential-bearing payload. | Retained and backed up indefinitely with no redaction, automatic expiry, or disk-pressure deletion. | Accessible for inspection/export only through manual administration outside Jarvis. It is classified at the highest sensitivity of any payload and is not the security audit. |

### History retrieval, deletion, and secret-bearing content

Accessible history is indexed and searched only on the Ubuntu host using SQLite
full-text search and deterministic filters. V1 does not create semantic
embeddings or send the archive to an external retrieval provider. Jarvis may
automatically retrieve a bounded relevant subset of non-deleted history. When it
uses an earlier conversation, it discloses that use with a compact conversation-
date and message-timestamp pointer.

Moving history to the deleted directory immediately removes every Jarvis-
readable derivative: full-text index entries, cached excerpts, retrieval
pointers, and generated summaries. Content-free tombstones remain so message IDs
and audit references are still interpretable. Deleted content and administrative
backups are inaccessible to Jarvis and recoverable only through manual
administration.

Authorized messages are stored verbatim even when they contain an API key,
password, or other credential-class secret. An explicit remember instruction may
also place such a value in plaintext durable assistant memory. Detected secret-
bearing messages and memories remain deterministically searchable/inspectable but
are excluded from automatic retrieval, retrieval indexes, and model context
unless the operator explicitly selects the exact record. Secrets deliberately
provided through the credential path remain in service-specific plaintext
credential files and must not be copied into ordinary state or routine backups.

### Audit and content exclusion

The permanent audit requires metadata-only events for inbound admission and
authorization; session/request lifecycle changes; history and durable-memory
access or mutation; connected-service reads; tool proposals and outcomes;
approvals and rejections; permission changes; terminal executions; outbound-
message attempts; restarts; and security or degraded-state failures. Routine
internal model reasoning belongs only to diagnostic telemetry.

The audit never stores raw message bodies, connected-service content,
credentials, secrets, or unbounded tool/command output. Tool calls and terminal
actions appear only in a bounded form with sensitive arguments redacted. Raw
prompts, retrieved content, tool results, command output, and model working
context leave request working storage after use but remain in the permanent full
diagnostic trace. Text actually sent to the operator is durable conversation
history as well.

Model-provider persistent conversation storage is disabled. Each model call
receives only bounded current context plus explicitly selected or locally
retrieved history. Provider response/conversation identifiers may be retained as
correlation metadata but never as the sole copy or authority for Jarvis history,
memory, approvals, or lifecycle state.

## Comments

- Amended on 2026-08-02 after review of the original interview evidence. The
  prior payload-free 30-day diagnostic-trace row contradicted the authorized
  operator's confirmed choice to retain full traces indefinitely. This answer now
  records full unredacted trace payloads, indefinite retention, backup inclusion,
  manual-administration-only access, no automatic deletion, and WhatsApp silence
  when required audit append is unavailable.
- Ticket 10 superseded the encrypted-secret-store choice with service-specific
  plaintext credential files so services restart unattended. Static files are
  root-owned; a connector that must rotate a credential owns only its private
  credential directory.
