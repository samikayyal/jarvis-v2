# 25 — Execute through the native Ubuntu worker contract

**What to build:** A production-shaped but unactivated native Ubuntu worker accepts only authenticated Ubuntu-bound actions and runs one bounded least-privileged process scope without Docker privilege or host substitution.

**Blocked by:** 12 — Reconcile interruption, outbox state, and ambiguous outcomes.

**Status:** ready-for-agent

- [ ] The worker verifies authenticated local identity and rejects actions bound to another host or an unavailable/degraded worker.
- [ ] It runs a non-interactive bounded process scope, streams tagged bounded output, and cancels the complete process tree.
- [ ] Contract verification uses controlled local inputs and does not install a service, activate credentials, expose a listener, or perform trust-critical changes.

