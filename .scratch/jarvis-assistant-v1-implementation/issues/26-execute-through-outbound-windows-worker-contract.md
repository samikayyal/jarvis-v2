# 26 — Execute through the outbound Windows worker contract

**What to build:** A production-shaped but unactivated Windows worker establishes an outbound private-overlay mTLS session, binds its authenticated identity to Windows, and runs one bounded Job Object-controlled action without queueing or failover.

**Blocked by:** 12 — Reconcile interruption, outbox state, and ambiguous outcomes.

**Status:** complete

- [x] Certificate and application identities must both match the registered Windows worker before an action is accepted.
- [x] Execution is non-interactive, deadline- and output-bound, and cancellation terminates the complete Job Object process tree.
- [x] Offline, disconnect, reconnect, and identity-mismatch behavior is contract-tested without installing a service, provisioning live credentials, or changing the network.

## Comments

- Implemented on `codex/ticket-26-windows-worker` with the outbound TLS 1.3 mTLS configuration boundary, dual certificate/application identity admission, heartbeat and reconnect fencing, and a suspended-before-assignment native Windows Job Object executor. Contract tests use controlled sessions only; no service, credential, listener, or network activation is included.

