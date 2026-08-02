## Destination

OpenWA runs persistently on `samik@192.168.1.250` as a LAN-administered messaging gateway for one dedicated WhatsApp account. The account can send and receive verified test messages through the active Baileys engine, and the pairing survives a controlled OpenWA recreation without a new QR.

## Notes

- Scope this effort to the messaging layer; AI behavior and assistant workflows come later.
- Prefer a lean production deployment suitable for 4 GB RAM: SQLite, local persistent storage, and no PostgreSQL, Redis, MinIO, or Traefik.
- The API and dashboard are LAN-only. WhatsApp messaging remains usable from anywhere because the laptop makes the outbound WhatsApp connection.
- Use the dedicated, already-active WhatsApp account and pair it through the phone's linked-device QR flow.
- Baileys is the sole active engine. `whatsapp-web.js` was tried first but WhatsApp invalidated both web pairings; retain the engine switch as a manual, one-engine-at-a-time procedure.
- OpenWA starts after reboot. Pin the installed revision and perform upgrades manually and deliberately.
- This map explicitly carries execution through the destination after its prerequisite decisions are resolved.
- Use `uv` for any Python work in this repository.

## Decisions so far

<!-- Resolved tickets are indexed here. -->

- OpenWA is pinned to `v0.12.1`; its minimal production contract is the default SQLite/local-volume stack on port 2785, with separate retained auth stores for whatsapp-web.js and Baileys ([research](research/openwa-v0.12.1-deployment-contract.md), [ticket 01](issues/01-confirm-current-openwa-deployment-contract.md)).
- The laptop is conditionally suitable: CPU, disk, addressing, SSH, and outbound access are adequate, but Docker is absent, the 512 MiB swap is full, the firewall is inactive, and the graphical desktop consumes avoidable headroom ([laptop-readiness inspection](issues/02-inspect-laptop-readiness.md)).
- The runtime is a lean single-container OpenWA `v0.12.1` deployment with an immutable image digest, 4 GiB total swap, persistent local state, exact `192.168.1.250:2785` binding, and UFW allowing SSH plus port 2785 from the full `192.168.1.0/24` LAN ([minimal runtime layout](issues/03-lock-minimal-runtime-layout.md)).
- OpenWA is provisioned and healthy at `http://192.168.1.250:2785`; Docker, swap, firewall, persistent state, secret permissions, boot restart, exact digest pinning, LAN reachability, and authenticated API access are verified ([provisioning record](issues/04-provision-and-secure-openwa.md)).
- The dedicated account is paired as the `jarvis` session and OpenWA reports it `ready` under `whatsapp-web.js`; pairing secrets and account identifiers remain outside the tracker ([pairing record](issues/05-pair-dedicated-whatsapp-account.md)).
- `whatsapp-web.js` did not meet the persistence requirement, while Baileys remained `ready`, passed inbound/outbound text verification, and restored without a QR after a controlled recreation ([messaging and persistence verification](issues/06-verify-messaging-and-persistence.md)).
- The reversible engine switch uses separate retained auth roots, Compose validation and recreation, and readiness/message/persistence checks; rollback to web currently requires re-pairing and is not recommended after two WhatsApp `LOGOUT` invalidations ([Baileys fallback procedure](issues/07-prepare-baileys-fallback.md)).

## Not yet specified

- How the later AI assistant consumes inbound messages and produces replies.
- Whether the future assistant uses cloud models, local models, or a hybrid.
- Whether administration should later be available securely from outside the LAN.
- What application-level authorization rules the future assistant applies to senders and group chats.

## Out of scope

- AI prompts, tools, memory, autonomous behavior, and reply logic.
- Public exposure, router port forwarding, and public reverse-proxy/TLS setup.
- Multiple WhatsApp accounts or concurrent OpenWA sessions.
- PostgreSQL, Redis, MinIO, and the full OpenWA infrastructure profile.
- Running a local LLM on this 4 GB laptop.
