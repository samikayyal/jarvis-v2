# 26 — Execute through the outbound Windows worker contract

**What to build:** A production-shaped but unactivated Windows worker establishes an outbound private-overlay mTLS session, binds its authenticated identity to Windows, and runs one bounded Job Object-controlled action without queueing or failover.

**Blocked by:** 12 — Reconcile interruption, outbox state, and ambiguous outcomes.

**Status:** ready-for-agent

- [ ] Certificate and application identities must both match the registered Windows worker before an action is accepted.
- [ ] Execution is non-interactive, deadline- and output-bound, and cancellation terminates the complete Job Object process tree.
- [ ] Offline, disconnect, reconnect, and identity-mismatch behavior is contract-tested without installing a service, provisioning live credentials, or changing the network.

