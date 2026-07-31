# OpenWA messaging gateway

## Purpose

This deployment provides a persistent, LAN-administered WhatsApp messaging
gateway for one dedicated account. It covers the messaging layer only: pairing,
inbound and outbound transport, persistence, administration, and recovery.

AI prompts, assistant behavior, tools, memory, sender authorization, and public
administration are not part of this completed slice.

## Current state

As verified on 2026-07-31:

- OpenWA `v0.12.1` runs as one `openwa-api` container on the dedicated laptop.
- The image is pinned by immutable digest; upgrades are manual.
- Baileys is the sole active messaging engine.
- The named session is paired and reports `ready` independently of HTTP health.
- Inbound and outbound text messaging has been proven end to end.
- The outbound verification reply was confirmed on the receiving phone.
- Baileys authentication survived a controlled Compose recreation without a new
  QR code.
- OpenWA and Docker are configured to recover automatically after a host boot.
- Administration is available only from the trusted home LAN. The laptop makes
  the outbound WhatsApp connection, so messaging does not depend on the sender
  being on that LAN.

`whatsapp-web.js` is not a viable active engine for this account at present.
WhatsApp issued `LOGOUT` shortly after pairing in two independent trials, using
both OpenWA's remotely pinned web build and WhatsApp's first-party web build.
Baileys was selected after those failures; see
[ADR-0001](../adr/0001-use-baileys-as-openwa-engine.md).

## Documentation map

- [Deployment and security contract](deployment.md) describes the installed
  runtime, persistence boundary, configuration, network policy, and upgrade
  posture.
- [Operations and recovery runbook](operations.md) contains health, readiness,
  messaging, restart, engine-switch, backup, and failure-handling procedures.
- [Verification record](verification.md) records what was actually demonstrated
  and the measured resource envelope.
- The [Wayfinder map](../../.scratch/openwa-messaging-service/map.md) and its
  resolved tickets retain the detailed chronology and evidence.

## Sensitive-data rule

Never place any of the following in this repository, issue tracker, terminal
transcript, or chat:

- phone numbers, chat IDs, or account identifiers;
- QR payloads or screenshots;
- API keys or the API-key pepper;
- raw WhatsApp authentication files;
- unredacted message exports or logs.

Secrets remain in the root-owned deployment environment, and pairing state
remains in the persistent Docker volume.
