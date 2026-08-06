# 16 — Complete the state-bound Google OAuth lifecycle

**What to build:** The narrow Google OAuth callback completes an operator-initiated authorization-code flow for exactly the configured identity, with single-use state and connector-owned token replacement, while exposing no general Jarvis control surface.

**Blocked by:** 03 — Enforce append-only audit admission and safe inspection; 04 — Retain complete diagnostic traces and enforce trace capacity.

**Status:** ready-for-agent

- [x] Short-lived state is single-use, callback responses are content-free, and the connected Google identity must match the configured identity.
- [x] Token replacement is atomic inside the connector credential boundary and does not place credentials in ordinary state, logs, command lines, or repository content.
- [ ] Wrong identity, missing scope, revocation, `invalid_grant`, and reconnection invalidate stale actions and fail safely using controlled OAuth doubles.

## Comments

- 2026-08-06: OAuth state transitions advance a durable connection generation, but no Google proposal or dispatcher exists before Tickets 18 and 19 to bind and recheck that generation at approval and dispatch. The lifecycle must not be marked complete on stale-action invalidation until that enforcing seam is implemented.
