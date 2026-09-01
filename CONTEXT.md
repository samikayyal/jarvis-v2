# Jarvis personal assistant

Jarvis is one native Ubuntu personal assistant runtime behind an independently
operated OpenWA messaging gateway. It serves one authorized operator and accepts
only direct WhatsApp text admitted by the gateway handoff.

## Language

**Messaging gateway**:
The persistent OpenWA boundary that receives and sends WhatsApp messages for the
dedicated account. It owns transport, message storage, session readiness, and
pairing state; it does not decide how Jarvis answers.
_Avoid_: Personal assistant runtime, model-and-tool loop

**Messaging layer**:
OpenWA pairing, transport, persistence, readiness, and recovery. It remains
operationally separate from assistant behavior.
_Avoid_: Assistant logic, prepared tool

**Dedicated account**:
The single WhatsApp account assigned to the messaging gateway.
_Avoid_: Authorized operator, any sender

**Named session**:
The gateway's human-readable logical connection to the dedicated account. It is
messaging-ready only when its state is `ready`. API routes may instead require
the distinct internal session ID.
_Avoid_: Container health, internal session ID

**Pairing state**:
Confidential authorization material in the complete `openwa-data` volume that
lets the named session reconnect without a new QR code.
_Avoid_: API key, runtime credential

**Personal assistant runtime**:
The single native Ubuntu process that receives admitted text, handles
deterministic commands, and runs ordinary requests through the model-and-tool
loop.
_Avoid_: Messaging gateway, control plane, capability broker

**Authorized operator**:
The one person allowed to invoke the personal assistant runtime, identified by
one configured WhatsApp number. Other senders, groups, self-authored messages,
and media never enter assistant work.
_Avoid_: Dedicated account, any WhatsApp sender

**Admitted text**:
An authenticated, valid `message.received` direct-text event for the configured
internal session and authorized operator that is not a retained duplicate.
_Avoid_: Any authenticated event, queued request

**Deterministic command**:
An exact slash-prefixed operator message handled by application code without
asking the model to interpret it. The supported commands are `/help`, `/new`,
`/status`, `/cancel`, `/model`, `/reasoning`, `/permissions`, and
`/forget-permission`.
_Avoid_: Prompt, prepared tool, ordinary request

**Ordinary request**:
Admitted text that is not a deterministic command and enters the sequential
model-and-tool loop.
_Avoid_: Background task, queued request

**Working session**:
The in-memory conversation context for the authorized operator. It ends on
`/new`, configured inactivity, service restart, or the configured token limit.
It is never restored from a provider or local archive.
_Avoid_: OpenWA named session, durable conversation

**Active request**:
The one ordinary request currently being processed. A concurrent ordinary
message is refused rather than queued or joined.
_Avoid_: Working session, pending action

**Model-and-tool loop**:
The sequential direct OpenAI Responses cycle that sends the complete current
working-session transcript, executes one prepared tool call at a time, returns
tool results, and ends with operator-facing text. Parallel tool calling is
disabled.
_Avoid_: Deterministic command, provider conversation object

**Prepared tool**:
One deliberately implemented operation exposed to the model-and-tool loop. The
current prepared tools are `read_vault` and `run_terminal`.
_Avoid_: Arbitrary capability, connector framework

**Pending action**:
The one exact terminal command waiting indefinitely for the authorized operator
to choose `1` (approve once), `2` (approve and save), `9` (reject), or `/cancel`.
All other messages are silently ignored while it is pending.
_Avoid_: Active request, saved permission, queued action

**Saved permission**:
A non-secret host-plus-literal-command-prefix rule stored only in the
`[saved_permissions]` section of `jarvis.toml`. It never grants authority across
hosts or to a changed command prefix.
_Avoid_: Wildcard permission, credential, approval for one action

**Runtime trace**:
Rotating verbatim JSON Lines evidence for admitted authorized messages, complete
OpenAI request/response payloads, tool activity, terminal activity, approvals,
errors, timing, and outbound replies. Trace failure warns but does not block the
request.
_Avoid_: Sanitized application log, hidden model reasoning

## Invariants

- OpenWA is the only messaging gateway. Its pinned image, `openwa-data` volume,
  Baileys pairing, dedicated account, private exposure, and operating runbooks
  are not assistant-runtime assets.
- Container health is necessary but insufficient. The exact configured named
  session must also be `ready`; `LOGOUT` or a fresh QR is a hard stop.
- The webhook authenticates the exact raw body and acknowledges promptly.
  Excluded or malformed traffic is ignored before runtime work.
- There is one working session, at most one active request, and at most one
  pending action. There is no request queue or parallel tool execution.
- Runtime sessions are memory-only. Restart discards the working session, active
  request, and pending action; nothing resumes or replays.
- The seven-day OpenWA message-ID cache prevents duplicate admission. It is not
  conversation history.
- Jarvis reads credentials only from `.env`, non-secret settings and saved
  permissions from `jarvis.toml`, and its editable prompt from `SYSTEM.md`.
  Jarvis never edits `.env` or `SYSTEM.md`.
- Configured simple read-only command prefixes may run automatically. Compound
  shell structure, scripts, unmatched commands, and mutating commands require
  exact approval or a matching saved permission.
- Ubuntu execution uses local subprocesses. Windows execution uses ordinary
  OpenSSH over Tailscale with a dedicated key and pinned host key. There are no
  custom workers.
- Terminal commands and outbound message chunks execute once. Timeouts or
  transport failures after an attempt are uncertain and are never retried
  automatically.
- Retrieved vault content and terminal output are untrusted data. They cannot
  grant authority, alter policy, select another host, or approve work.
- Runtime traces are sensitive verbatim data. Hidden model reasoning is not
  available and is never claimed as trace content.

## Ownership boundaries

| Boundary | Owns | Must not own |
| --- | --- | --- |
| OpenWA | WhatsApp transport, message persistence, pairing, named-session readiness | Assistant decisions, command permissions, model transcript |
| Personal assistant runtime | Admission filtering, sessions, commands, model-and-tool loop, approvals, replies | OpenWA pairing or container lifecycle, durable conversation history |
| `.env` | OpenAI and OpenWA credentials | Non-secret settings, saved permissions |
| `jarvis.toml` | Non-secret limits, paths, identities, read-only prefixes, saved permissions | Credentials, message bodies, command output |
| `SYSTEM.md` | Editable assistant instruction | Credentials, saved permissions |
| Runtime data directory | Seven-day deduplication cache and verbatim rotating trace | OpenWA data or pairing state |

## Operational definition of active

Jarvis is active only when the native service is enabled and running on its exact
private bridge listener, OpenWA is healthy with the configured named session
`ready`, the single webhook targets that listener, and a real authorized message
produces one expected phone reply. A listener check or OpenWA `/ready` response
alone is not end-to-end acceptance.
