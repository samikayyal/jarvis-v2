# 19 — Create and update Calendar events through exact approval

**What to build:** Calendar insert, update, and reviewed-patch proposals freeze the complete resulting event and dispatch it once only after exact approval.

**Blocked by:** 08 — Present oversized proposals through the universal envelope; 17 — Read bounded Gmail, Calendar, and Drive content.

**Status:** ready-for-agent

- [ ] Proposals freeze the calendar, event identity, complete resulting content, attendees, recurrence, visibility, reminders, ETag, and notification effects.
- [ ] Approval dispatches exactly the stored insert, update, or reviewed patch once and rejects changed or stale proposals.
- [ ] Array replacement hazards, destructive/excluded operations, concurrent state changes, and ambiguous outcomes fail closed without automatic retry.

