# 11 — Dispatch bounded work through a controlled worker gateway

**What to build:** A frozen host-bound terminal action reaches only the selected authenticated worker identity and runs within deterministic interaction, time, output, cancellation, and outcome bounds, without queueing or host failover.

**Blocked by:** 10 — Create, match, list, and revoke exact command permissions.

**Status:** ready-for-agent

- [ ] Dispatch verifies the selected host identity immediately before execution and never substitutes or queues for another host.
- [ ] Execution is non-interactive, deadline-bound, output-bound, and cancellable across the complete process tree.
- [ ] Compound partial outcomes, disconnects, and definite-versus-unknown results are represented accurately and never automatically retried when a side effect may have occurred.

