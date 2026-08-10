# 27 — Assemble and verify the unactivated deployment bundle

**What to build:** Reviewed and pinned development artifacts assemble the completed control plane, separated connectors, private networks, native workers, configuration validation, health checks, administration, logging, and resource bounds into an unactivated deployment bundle.

**Blocked by:** 13 — Connect the control plane to the pinned OpenWA contract; 15 — Invoke Codex as a bounded, independently verified specialist; 18 — Send and reply to Gmail through exact approval; 19 — Create and update Calendar events through exact approval; 21 — Revoke conversation access through confirmed deletion; 22 — Manage explicit durable assistant memory; 24 — Commit and push one exact knowledge-vault patch; 25 — Execute through the native Ubuntu worker contract; 26 — Execute through the outbound Windows worker contract.

**Status:** ready-for-agent

- [ ] The bundle pins all artifacts and validates topology, identities, private/non-published networks, configuration schema, credential mounts, readiness, logs, and aggregate resource limits.
- [x] OpenWA remains an independent deployment and only its future two-member private handoff network is described, not activated.
- [x] Isolated verification installs no host service, provisions no real credential, changes no firewall/network state, and attaches no production OpenWA or worker.

## Comments

- Partial implementation on `codex/ticket-27-deployment-bundle`: the manual-activation-profile Compose description, distinct service identities, least-privilege container boundaries, pinned build inputs, network/resource/credential policy, and offline adverse-case verifier are implemented. Final assembly remains blocked because the completed application artifacts expose in-process Python ports but no production inter-service protocols or role-specific composition roots; verifier-only containers are not accepted as runnable services. OpenWA and both native workers remain outside the Compose project, and no service, credential, firewall, production network, or live dependency was activated during verification.

