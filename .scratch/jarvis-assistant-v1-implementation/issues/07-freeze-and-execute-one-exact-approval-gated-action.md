# 07 — Freeze and execute one exact approval-gated action

**What to build:** One typed proposal becomes a frozen pending action whose exact stored payload can be consumed and dispatched once only by a deterministic confirmation from its owning operator, session, and request.

**Blocked by:** 03 — Enforce append-only audit admission and safe inspection; 05 — Manage working sessions, `/status`, `/cancel`, and `/new`.

**Status:** complete

- [ ] The pending action freezes its action ID, digest, preview, payload, ownership, and ten-minute expiry without allowing later mutation.
- [ ] Exact whole-message confirmation atomically records approval and dispatches the stored payload at most once.
- [ ] Rejection, unrelated text, expiry, cancellation, restart invalidation, altered approval, and replay dispatch nothing.
