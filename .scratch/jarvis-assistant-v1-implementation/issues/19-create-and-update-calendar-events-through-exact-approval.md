# 19 — Create and update Calendar events through exact approval

**Historical scope:** Calendar insert, update, and reviewed-patch proposals once froze the complete resulting event and dispatched it only after exact approval. This is not Jarvis v1 work.

**Historical dependencies:** 08 — Present oversized proposals through the universal envelope; the former Calendar-inclusive version of 17 — Read bounded Google content.

**Status:** wontfix

Calendar was removed from the Jarvis v1 product surface on 2026-08-23 and deferred to a later Jarvis version. The completed implementation below is retained only as historical context; v1 no longer requests Calendar scopes, exposes Calendar tools, accepts Calendar proposals, or routes Calendar actions.

- [x] Proposals freeze the calendar, event identity, complete resulting content, attendees, recurrence, visibility, reminders, ETag, and notification effects.
- [x] Approval dispatches exactly the stored insert, update, or reviewed patch once and rejects changed or stale proposals.
- [x] Array replacement hazards, destructive/excluded operations, concurrent state changes, and ambiguous outcomes fail closed without automatic retry.

