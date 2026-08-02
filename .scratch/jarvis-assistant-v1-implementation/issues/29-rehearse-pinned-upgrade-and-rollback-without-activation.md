# 29 — Rehearse pinned upgrade and rollback without activation

**What to build:** The unactivated bundle can validate and rehearse a pinned replacement release and return to the previous compatible release and state using isolated restored data, without changing active Jarvis or OpenWA services.

**Blocked by:** 28 — Back up and restore Jarvis state, audit, and traces.

**Status:** ready-for-agent

- [ ] Maintenance admission stop, immutable artifact validation, configuration validation, and isolated migration rehearsal are demonstrable.
- [ ] A known message window can be reconciled with durable deduplication and without replaying outcome-unknown work.
- [ ] Forced rehearsal failure restores the previous compatible release and state, and no active service or trust-critical configuration is changed.

