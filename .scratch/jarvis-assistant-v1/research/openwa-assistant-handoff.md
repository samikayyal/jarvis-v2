# OpenWA assistant handoff

Research date: 2026-08-01
Scope: the pinned OpenWA v0.12.1/Baileys deployment and the boundary between the existing messaging gateway and Jarvis Assistant V1.

## Decision

Use one OpenWA webhook for `message.received` as the live inbound boundary. The assistant receiver must verify the webhook signature, acknowledge quickly with HTTP 2xx, atomically deduplicate on `(sessionId, data.id)`, then process only an authorized, direct, text message. Use the inbound message's `data.id` as `quotedMessageId` when calling OpenWA's reply route. Keep OpenWA's gateway and its current direct webhook delivery behavior unchanged.

Polling is a recovery/reconciliation mechanism only. OpenWA exposes message history, but the pinned API does not provide a webhook-equivalent cursor or delivery watermark; therefore polling must not become the primary trigger or be treated as an exactly-once stream.

This fits the local V1 boundary: reactive, text-only, one allowlisted personal WhatsApp number, with the messaging layer responsible for transport and persistence rather than assistant policy. The deployed gateway is Baileys-only and has already been verified healthy with the named session independently ready; the OpenWA API remains a transport boundary, not the assistant runtime. [Jarvis context](../../../CONTEXT.md), [local OpenWA deployment](../../../docs/openwa/README.md#current-state), [local verification](../../../docs/openwa/verification.md#baileys-acceptance-result)

## 1. Inbound contract

### Preferred route: signed webhook

Configure the existing session's webhook with:

```json
{
  "url": "https://<assistant-receiver>/openwa/inbound",
  "events": ["message.received"],
  "secret": "<shared-secret>"
}
```

The URL and secret above are placeholders for implementation configuration; this research does not change the deployed gateway or retain a secret. OpenWA's supported event list includes `message.received`, and a webhook defaults to that event when no event list is supplied. The configured secret causes OpenWA to send an HMAC-SHA256 signature over the exact JSON request body. Verify the signature against the raw body before parsing it. [Webhook event DTO](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/webhook/dto/webhook.dto.ts#L33-L110), [signature and delivery headers](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/webhook/webhook.service.ts#L448-L531)

Each callback is an envelope of this shape:

```json
{
  "event": "message.received",
  "timestamp": "<dispatch-time ISO timestamp>",
  "sessionId": "<OpenWA internal session ID>",
  "idempotencyKey": "<OpenWA webhook idempotency key>",
  "deliveryId": "<OpenWA delivery ID>",
  "data": {
    "id": "<WhatsApp message ID>",
    "from": "<normalized sender JID>",
    "to": "<normalized account JID>",
    "chatId": "<normalized chat JID>",
    "body": "<text>",
    "type": "text",
    "timestamp": 0,
    "fromMe": false,
    "isGroup": false,
    "kind": "<message kind>",
    "quotedMessage": { "id": "<quoted message ID>", "body": "<quoted text>" }
  }
}
```

The `data` fields shown as optional or illustrative must be handled defensively. The pinned engine interface defines the neutral `IncomingMessage` fields; the Baileys mapper normalizes the raw JIDs and carries optional quoted, ephemeral, mention, media, and contact fields. The envelope `timestamp` is the webhook dispatch time; `data.timestamp` is the message timestamp in Unix seconds. [Webhook payload interface](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/webhook/webhook.service.ts#L37-L44), [neutral incoming message interface](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/engine/interfaces/whatsapp-engine.interface.ts#L65-L122), [Baileys normalization](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/engine/adapters/baileys-message-mapper.ts#L127-L234)

The receiver should perform these checks in order:

1. Verify the HMAC signature and reject unauthenticated requests.
2. Require `event == "message.received"`, a non-empty envelope `sessionId`, a non-empty `data.id`, and `data.fromMe == false`.
3. Require a direct message: `data.isGroup == false`, `data.chatId == data.from` after the gateway's normalization, and a canonical direct-message JID. Reject status broadcasts and group messages for V1.
4. Authorize `data.from` against the configured single allowlisted operator JID. Do not authorize from `pushName`, contact display text, `senderPhone`, message body, or the webhook URL. If the sender is represented only by an unresolved LID (`isLidSender`), fail closed until a stable allowlist comparison is available.
5. Require text-only content: `data.type == "text"` and a non-blank `data.body`. Ignore media, calls, reactions, status traffic, and other message kinds in V1. OpenWA may deliver a message while media is omitted by its limiter; V1 should not download or interpret it.
6. Atomically insert an assistant-ingress record keyed by `(envelope.sessionId, data.id)`. If that key already exists, return 2xx without invoking the assistant again. Only the first accepted record may enqueue assistant work.

For a direct Baileys message, the normalized mapper uses the chat JID as `from` and `chatId`, and the account JID as `to`. For group messages, `from` is the group JID and the actual participant is in `author`; that is another reason V1 should reject groups rather than infer authorization from `author`. The optional `contact.pushName` is presentation metadata, not an identity proof. [Baileys message mapping](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/engine/adapters/baileys-message-mapper.ts#L158-L234), [V1 scope](../../../CONTEXT.md)

### Event boundary versus history polling

OpenWA's session pipeline dispatches `message.received` after persistence, and the webhook dispatch is fire-and-forget. History synchronization persists older messages but does not call the live message callback or dispatch `message.received`. The Baileys adapter also filters non-notify messages and old pre-connection messages from the live inbound callback. This makes the webhook the correct trigger and avoids replaying history as new assistant requests. [Live message persistence and dispatch](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/session/session.service.ts#L1151-L1336), [Baileys live/history boundary](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/engine/adapters/baileys.adapter.ts#L1324-L1361), [history persistence without dispatch](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/engine/adapters/baileys.adapter.ts#L1931-L1964)

The fallback history API is `GET /api/sessions/:sessionId/messages`, where `:sessionId` is OpenWA's internal database/API session ID, not the human-readable session name. It supports `chatId`, `from`, `limit`, and `offset`; the pinned contract does not define a durable cursor, event watermark, or exactly-once polling order. Use it only to reconcile a known outage or inspect a bounded window, and apply the same `(sessionId, waMessageId)` assistant deduplication before triggering work. Do not poll continuously as a substitute for webhook acknowledgement. [Pinned message controller](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/message/message.controller.ts#L1-L45), [OpenAPI message history](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/openapi.json#L1332-L1391), [local session-ID warning](../../../docs/openwa/operations.md#message-history-and-e2e-text-check)

## 2. Identity and message IDs

Use the following fields for the handoff:

| Field | Meaning and handling |
|---|---|
| `envelope.sessionId` | OpenWA's internal session/API identifier. Keep it with the ingress record and use the same value for outbound API calls. Never substitute the human-readable session name. |
| `data.id` | The Baileys message key ID, exposed by OpenWA as the neutral message ID and stored as `waMessageId`. This is the primary message identity for assistant work. |
| `data.chatId` | Normalized conversation JID. For V1 direct messages it must equal `data.from`. |
| `data.from` | Normalized sender/conversation JID. For a direct message it is the sender's canonical JID and the allowlist comparison field. |
| `data.to` | Normalized account JID for an inbound direct message. It is useful as a consistency check, not as sender authorization. |
| `data.author` | Optional actual participant for group messages. V1 rejects groups, so this is not a substitute for the direct-message allowlist. |
| `data.timestamp` | Message occurrence timestamp, Unix seconds. Do not confuse it with the envelope's webhook dispatch timestamp. |
| `deliveryId` | A random ID for one webhook delivery, stable across that delivery's retry attempts. It is not the WhatsApp message ID. |
| `idempotencyKey` | OpenWA's event key. For `message.received`, the pinned utility derives it from session plus message ID, and the webhook service scopes it per webhook. Treat it as a useful transport key, but keep the assistant's own `(sessionId, data.id)` unique constraint as the authority. |

OpenWA's own message table has a composite unique constraint on `(sessionId, waMessageId)`, so the gateway has a persistence-level deduplication boundary. That does not make HTTP webhook delivery exactly once: a receiver can finish work and lose its response, after which OpenWA may retry. The assistant therefore needs its own atomic inbox/claim boundary keyed by the same logical message tuple. [Message entity and unique index](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/message/entities/message.entity.ts#L30-L89), [OpenWA message idempotency utility](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/modules/webhook/utils/idempotency.util.ts#L20-L43), [delivery ID generation](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/modules/webhook/utils/idempotency.util.ts#L107-L112)

## 3. Quoted-message metadata

`data.quotedMessage` is optional and, at the pinned Baileys mapping boundary, contains only:

```json
{ "id": "<quoted message ID>", "body": "<quoted text or extracted caption>" }
```

OpenWA creates it only when the normalized WhatsApp context has both a quoted message and a stanza ID. Its body extraction covers text and selected captions; it is not a complete copy of the quoted message and does not establish the quoted sender's identity. Treat the quoted body as display context only. Use `data.id`—the current inbound message ID—as `quotedMessageId` when replying to the user. [Baileys quoted-message extraction](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/engine/adapters/baileys.adapter.ts#L1861-L1929), [neutral quoted field](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/engine/interfaces/whatsapp-engine.interface.ts#L113-L122)

## 4. Outbound reply route

For a normal assistant response to an accepted inbound message, call:

```http
POST /api/sessions/<internal-session-id>/messages/reply
Authorization: <configured OpenWA operator authentication>
Content-Type: application/json

{
  "chatId": "<data.chatId>",
  "quotedMessageId": "<data.id>",
  "text": "<assistant response, at most 4096 characters>"
}
```

Use `POST /api/sessions/<internal-session-id>/messages/send-text` with `chatId` and `text` only when a threaded quote is intentionally not wanted. The two routes send directly through the active engine; the reply service makes a best-effort lookup of quoted text for local metadata and calls the engine's `replyToMessage`. The successful response contains an OpenWA message ID and timestamp, meaning the gateway accepted the send; it is not proof that WhatsApp delivered the message. [Message controller reply route](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/message/message.controller.ts#L210-L240), [reply DTO](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/modules/message/dto/message-actions.dto.ts#L106-L123), [send-text DTO and response](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/modules/message/dto/send-message.dto.ts#L20-L36), [outbound send persistence](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/modules/message/message.service.ts#L430-L625)

The assistant should store the returned outbound message ID and, if delivery tracking is needed, consume `message.sent`/`message.ack`/`message.failed` webhooks as status events. The local deployment verification confirmed an outbound row as `outgoing/sent` and confirmed the receiving phone, while also documenting that the row is not promoted to `delivered`; keep that distinction in assistant state. [Local verification](../../../docs/openwa/verification.md#baileys-acceptance-result)

## 5. Retry and acknowledgement behavior

### Inbound webhook delivery

The verified deployment has Redis and queueing disabled, so the active OpenWA path is direct webhook delivery. In that path, the pinned implementation:

- times out a callback after 10 seconds by default;
- treats any HTTP 2xx response as success;
- starts with `X-OpenWA-Retry-Count: 0`;
- uses the configured `retryCount` (default 3) as the maximum number of delivery attempts in the direct code path, not three attempts plus the first attempt;
- waits `retryDelay * attempt` between failures (default 5 seconds, then 10 seconds for the default three-attempt sequence);
- reuses the same JSON body, `idempotencyKey`, and `deliveryId` across those retries; and
- records a durable delivery failure after the attempts are exhausted.

The receiver must do signature verification, durable inbox insertion, and enqueueing quickly, then return 2xx. Do not run the LLM, Google calls, shell commands, or approval waits inside the webhook request. A non-2xx response should mean “the event was not durably accepted,” not “the assistant rejected this sender”; unauthorized or unsupported messages should still be durably recorded or safely discarded according to the assistant's policy and then acknowledged, otherwise OpenWA will retry them.

There is a small at-least-once boundary around process shutdown: OpenWA's direct dispatcher has a finite shutdown drain window, and the dispatch from the session service is fire-and-forget. The receiver's unique inbox claim is therefore required even when the gateway's own message record is unique. [Local queue/runtime contract](../../../docs/openwa/deployment.md#runtime-contract), [webhook configuration defaults](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/config/configuration.ts#L84-L87), [webhook timeout and retry settings](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/config/configuration.ts#L171-L212), [direct delivery retry path](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/modules/webhook/webhook.service.ts#L701-L777), [fire-and-forget dispatch](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/modules/session/session.service.ts#L1306-L1327)

If queueing is enabled in a future deployment, the queue processor adds its own BullMQ attempts/backoff behavior. That is outside the verified current contract; do not design V1 around it or enable Redis merely to connect the assistant. Re-check the live deployment contract before changing queue settings. [Pinned webhook queue branch](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/modules/webhook/webhook.service.ts#L537-L563)

### Outbound API retries

The send and reply DTOs have no client-supplied idempotency token. OpenWA sends directly through the engine, persists a pending outgoing record, and returns the accepted message ID after the engine accepts the send; a client-side retry after a timeout or connection break can therefore create a duplicate even if the first request succeeded. Do not automatically retry an ambiguous `send-text` or `reply` request. Record the result as unknown, reconcile using the outbound message event/history when possible, and require an explicit/manual retry for a new send. A clear validation/authentication failure may be surfaced without retrying. [Direct engine send path](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/message/message.service.ts#L31-L85), [reply send path](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/message/message.service.ts#L430-L461), [accepted-send persistence](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a485ed68587b25f53d2/src/modules/message/message.service.ts#L570-L625)

## 6. Idempotency boundary and implementation handoff

The handoff should preserve these separate boundaries:

| Boundary | Authority | Assistant behavior |
|---|---|---|
| Gateway message persistence | OpenWA unique `(sessionId, waMessageId)` | Do not write to OpenWA's database or infer assistant state from a duplicate webhook. |
| Webhook transport | `idempotencyKey` plus retry-stable `deliveryId`; 2xx acknowledgement | Verify the signature, acknowledge only after durable inbox acceptance, and tolerate duplicate delivery. |
| Assistant work admission | Assistant unique `(envelope.sessionId, data.id)` | One accepted inbound message creates at most one assistant job. Duplicate callbacks return 2xx and do no work. |
| Authorization | Canonical normalized direct-message `data.from` | Compare only with the single configured operator JID; reject groups, status traffic, unresolved identity, and non-text content. |
| Outbound send | OpenWA returned `messageId` plus optional status events | Treat 201 as gateway acceptance, not delivery; do not retry an ambiguous request automatically. |

The assistant receiver should keep the original envelope and processing state in its own durable store, with states such as `accepted`, `processing`, `completed`, and `failed`, while making the unique claim before invoking any side effect. A failed assistant job can be retried internally by the assistant because the inbound claim remains the same; that is separate from replaying the WhatsApp send. A reply should reference the original inbound `data.id`, so a retried assistant job must first consult its own outbound-attempt record before sending again.

No assistant behavior should depend on OpenWA's HTTP health endpoint alone: local operations explicitly require both container health and the named session's independent `ready` state. The handoff should therefore fail closed or queue locally when session readiness is not known, without restarting, re-pairing, switching engines, or changing the gateway's persistence. [Local readiness contract](../../../docs/openwa/deployment.md#health-and-messaging-readiness), [local operations](../../../docs/openwa/operations.md#routine-readiness-check)

## Confirmed facts, recommendations, and uncertainty

### Confirmed from pinned OpenWA source/API

- `message.received` is a supported webhook event and carries a common envelope with event, timestamp, session, idempotency, delivery, and data fields.
- Baileys produces the neutral message identity and routing fields described above, with optional quoted metadata containing only an ID and extracted body.
- Live inbound messages are persisted before webhook dispatch; history messages are persistence-only and are not live assistant triggers.
- OpenWA's own message deduplication is scoped to `(sessionId, waMessageId)`.
- Reply uses `chatId`, `quotedMessageId`, and text up to 4096 characters; a successful response is gateway acceptance, not WhatsApp delivery.
- Current direct webhook retries are timeout/non-2xx based and preserve the delivery identity across retries.

### Verified local facts

- The deployed runtime is OpenWA v0.12.1 with Baileys as the sole active engine, persistent state, queueing/Redis disabled, and separate session readiness verification.
- Local verification demonstrated inbound/outbound text persistence and controlled recreation back to healthy/ready. It did not establish an exactly-once assistant webhook contract or a durable polling cursor.

### Recommendations for the future implementation

- Configure only the inbound `message.received` webhook for the first assistant slice, with a secret and a receiver that acknowledges after an atomic inbox claim.
- Keep sender authorization and V1 text/direct-message policy in the assistant boundary; do not broaden the gateway's job.
- Use `/reply` with the current inbound message ID for normal responses and record the returned outbound message ID.
- Never blindly retry an ambiguous outbound API request.
- Use history polling only for bounded repair/reconciliation, with the same assistant dedupe key.

### Bounded uncertainty

- The exact deployed webhook registration, endpoint reachability, and secret value were intentionally not inspected or changed here. They must be confirmed during implementation without printing the secret or live identifiers.
- The API documents message history pagination parameters, but the pinned first-party contract does not establish a cursor/watermark suitable for exactly-once polling.
- The local verification covered controlled Compose recreation, not a full laptop reboot or an assistant receiver outage during webhook delivery. The receiver must therefore assume at-least-once delivery and handle shutdown/retry races.

## Sources

All upstream source links in this artifact point to OpenWA commit `31c5499a9beea1c5b460a4854ed68587b25f53d2`, the pinned v0.12.1 revision. The local deployment sources are the reviewed repository documents: [deployment](../../../docs/openwa/deployment.md), [application configuration](../../../docs/openwa/deployment.md#application-configuration), [health and readiness](../../../docs/openwa/deployment.md#health-and-readiness), [operations](../../../docs/openwa/operations.md#routine-status-check), [verification](../../../docs/openwa/verification.md), and [the earlier deployment contract](../../openwa-messaging-service/research/openwa-v0.12.1-deployment-contract.md).
