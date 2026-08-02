Type: research
Status: resolved
Blocked by:

## Question

At the pinned OpenWA deployment and Baileys engine, what exact inbound event or polling contract, sender identity fields, message identifiers, quoted-message metadata, outbound reply route, retry behavior, and idempotency boundary should connect the existing messaging gateway to assistant behavior without weakening gateway reliability?

## Answer

Resolved in [OpenWA assistant handoff research](../research/openwa-assistant-handoff.md). It defines the pinned Baileys `message.received` webhook contract, canonical sender and message-ID fields, quoted-message handling, `/reply` and `send-text` routes, direct-mode retry semantics, and the separate gateway versus assistant idempotency boundaries.

The implementation contract requires `data.fromMe == false`, direct non-group
text, a nonblank body, canonical authorized sender/chat identity, and the
configured internal session. Authenticated unauthorized or unsupported events
are durably dispositioned or safely discarded and acknowledged with `2xx` so
OpenWA does not retry routine V1 exclusions. Invalid signatures, malformed
events that cannot be dispositioned, and unwritable durable inbox state use the
explicit non-`2xx` results defined by the implementation specification.

## Comments

- Amended on 2026-08-02 to make the handoff research's rejection and
  acknowledgement requirements explicit in the resolved answer and point
  implementation to the complete ingress disposition matrix.
