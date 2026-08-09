# 12 — Reconcile interruption, outbox state, and ambiguous outcomes

**What to build:** Restart and recovery deterministically close admitted-but-unfinished work without resuming it and preserve enough durable attempt state to avoid duplicating connector, worker, or outbound side effects.

**Blocked by:** 03 — Enforce append-only audit admission and safe inspection; 04 — Retain complete diagnostic traces and enforce trace capacity; 07 — Freeze and execute one exact approval-gated action; 11 — Dispatch bounded work through a controlled worker gateway.

**Status:** complete

- [x] Restart marks active work interrupted, removes pending payloads, revokes session permissions, preserves persistent permissions, and clears request working data.
- [x] Durable outbox and dispatch records distinguish known-unattempted, attempted, confirmed, and unknown outcomes.
- [x] No interrupted model run, proposal, connector call, terminal process, or possibly successful side effect resumes or retries automatically.

