# 27 — Assemble and verify the unactivated deployment bundle

**What to build:** Reviewed and pinned development artifacts assemble the completed control plane, separated connectors, private networks, native workers, configuration validation, health checks, administration, logging, and resource bounds into an unactivated deployment bundle.

**Blocked by:** 13 — Connect the control plane to the pinned OpenWA contract; 15 — Invoke Codex as a bounded, independently verified specialist; 18 — Send and reply to Gmail through exact approval; 19 — Create and update Calendar events through exact approval; 21 — Revoke conversation access through confirmed deletion; 22 — Manage explicit durable assistant memory; 24 — Commit and push one exact knowledge-vault patch; 25 — Execute through the native Ubuntu worker contract; 26 — Execute through the outbound Windows worker contract.

**Status:** complete

- [x] The bundle pins all artifacts and validates topology, identities, private/non-published networks, configuration schema, credential mounts, readiness, logs, and aggregate resource limits.
- [x] OpenWA remains an independent deployment and only its future two-member private handoff network is described, not activated.
- [x] Isolated verification installs no host service, provisions no real credential, changes no firewall/network state, and attaches no production OpenWA or worker.

## Comments

- Completed on `codex/ticket-27-deployment-bundle`: the pinned manual-activation Compose bundle now runs ten role-specific composition roots over authenticated, per-link and per-operation service protocols; validates the active configuration before readiness; preserves separated state, trace, credential, network, OAuth, OpenWA, vault, and native-worker boundaries; and includes real local process, Git, Unix-worker, and TLS 1.3 Windows-worker verification. OpenWA and both native workers remain independently activated native/external services. Verification created no production credential, service, firewall rule, production network, worker attachment, or OpenWA handoff.
- PR #16 review repair: the bundle now runs a tenth, networkless deleted-conversation archive role so the broker has only an authenticated write-only IPC client; composes the pinned Codex CLI into orchestration over a read-only Git workspace with independent snapshots; provides broker-authenticated manual Google authorization and disconnect commands; and requests `openid` with the matching Google userinfo egress host.
- PR #16 second review repair: the bundle now preserves configured operation deadlines and valid terminal envelopes over concurrent authenticated service admission; bounds Windows handshakes; keeps archive health responsive; validates the configured vault origin and disables Git hooks; enforces the low-disk floor and authoritative Google disconnect state; exposes local-only audit administration; installs Codex from an integrity lock; excludes credentials from the image context; and routes OpenWA plus connector egress only through reviewed network boundaries.

