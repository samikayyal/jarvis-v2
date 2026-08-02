# 13 — Connect the control plane to the pinned OpenWA contract

**What to build:** Production-shaped inbound and outbound adapters implement the pinned Baileys/OpenWA contract while automated verification remains local and production attachment remains excluded.

**Blocked by:** 02 — Enforce inbound admission and replay protection; 08 — Present oversized proposals through the universal envelope; 12 — Reconcile interruption, outbox state, and ambiguous outcomes.

**Status:** ready-for-agent

- [ ] The adapters use the signed webhook, internal session ID, configured operator conversation, reply correlation, and returned outbound identifiers correctly.
- [ ] Container health and named-session readiness remain distinct, and outbound content respects the 4,096-character envelope.
- [ ] The contract double proves definite and ambiguous delivery behavior without connecting to or changing the live OpenWA deployment.

