---
status: accepted
date: 2026-07-31
---

# Use Baileys as the OpenWA messaging engine

## Context

The gateway needs one persistent WhatsApp session on a 4 GB-class laptop.
OpenWA `v0.12.1` supports `whatsapp-web.js` and Baileys, with separate retained
authentication roots. The original plan preferred `whatsapp-web.js` and kept
Baileys as a fallback.

The web engine was paired twice. Both sessions reached `ready`; the first also
passed inbound and outbound text testing. WhatsApp then issued `LOGOUT` within
minutes in both trials, once with OpenWA's remotely pinned web build and once
with WhatsApp's first-party served build. Neither invalidation was triggered by
a service restart. OpenWA correctly deleted the invalid credentials.

Baileys subsequently reached `ready`, remained stable through observation,
passed inbound/outbound text verification, and restored its pairing after a
controlled Compose recreation without a new QR. Its settled memory footprint
was substantially below the Chromium-backed synchronization footprint.

## Decision

Use Baileys as the sole active OpenWA messaging engine for this deployment.
Keep `ENGINE_TYPE=baileys` pinned in Compose and retain Baileys authentication
under `/app/data/baileys` in the `openwa-data` volume.

Retain the separate whatsapp-web.js auth path and a reversible manual
engine-switch procedure, but do not run both engines simultaneously. A web
rollback currently requires new pairing because WhatsApp invalidated and
OpenWA deleted both prior web credentials.

## Consequences

- The verified production path no longer requires Chromium, reducing the
  steady-state resource burden on the laptop.
- Baileys pairing state is independent of whatsapp-web.js state and must be
  protected as part of the complete `openwa-data` volume.
- Health monitoring must check both container health and authenticated session
  state.
- Operators must use the session's internal ID for message-history and send
  routes; the human-readable session name is not interchangeable there.
- Retrying whatsapp-web.js is an explicit reassessment, not an automatic
  failover. Repeated re-pairing after `LOGOUT` is avoided because WhatsApp may
  throttle linked-device attempts.
- Only one account and one engine are supported by this deployment contract.

## Evidence

- [Messaging and persistence verification](../../.scratch/openwa-messaging-service/issues/06-verify-messaging-and-persistence.md)
- [Engine-switch procedure](../../.scratch/openwa-messaging-service/issues/07-prepare-baileys-fallback.md)
- [Pinned OpenWA deployment research](../../.scratch/openwa-messaging-service/research/openwa-v0.12.1-deployment-contract.md)
