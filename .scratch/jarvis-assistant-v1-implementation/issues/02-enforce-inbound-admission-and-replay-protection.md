# 02 — Enforce inbound admission and replay protection

**What to build:** Invalid or unsupported inbound events cannot create assistant work or reveal connected capabilities, while duplicate authenticated events are acknowledged without duplicating state, replies, approvals, or dispatches.

**Blocked by:** 01 — Establish the signed-message control-plane tracer bullet.

**Status:** complete

- [ ] Signature, session, event, message, direct-chat, text, identity, and authorized-operator checks produce the specified durable ingress dispositions.
- [ ] Media, reactions, groups, status events, unresolved identities, and unauthorized senders create no request or assistant reply.
- [ ] Replaying the same `(session ID, message ID)` cannot duplicate work or any observable side effect.
