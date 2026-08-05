# 05 — Manage working sessions, `/status`, `/cancel`, and `/new`

**What to build:** An admitted request owns one working session and one active request, with deterministic status, cancellation, and clean-session transitions that preserve the V1 single-request and single-pending-action boundaries.

**Blocked by:** 01 — Establish the signed-message control-plane tracer bullet.

**Status:** complete

- [ ] `/status` exposes only the safe configured session, request, pending-action, permission, and readiness view.
- [ ] `/cancel` stops active work, invalidates its pending action, and prevents late results or dispatches from escaping the cancellation boundary.
- [ ] `/new` atomically ends current work, revokes session permissions, invalidates pending state, and starts a clean working session without deleting durable state.
