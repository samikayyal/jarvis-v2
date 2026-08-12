# 29 — Rehearse pinned upgrade and rollback without activation

**What to build:** The unactivated bundle can validate and rehearse a pinned replacement release and return to the previous compatible release and state using isolated restored data, without changing active Jarvis or OpenWA services.

**Blocked by:** 28 — Back up and restore Jarvis state, audit, and traces.

**Status:** complete

- [x] Maintenance admission stop, immutable artifact validation, configuration validation, and isolated migration rehearsal are demonstrable.
- [x] A known message window can be reconciled with durable deduplication and without replaying outcome-unknown work.
- [x] Forced rehearsal failure restores the previous compatible release and state, and no active service or trust-critical configuration is changed.

## Comments

- Implemented the replacement-runtime-only administrative rehearsal command with exact previous/replacement artifact validation, pre-change backup/admission-stop binding, isolated restore and startup migration, bounded inbox/request/pending-action/dispatch/outbound recovery, persistent isolated audit/degraded evidence, and forced rollback to the previous compatible release and state.
- Verified repository formatting and lint, 115 focused deployment/backup/recovery tests, a clean parallel standards/spec review, and the single final full-suite run: 720 passed, 2 skipped in 750.34 seconds.

