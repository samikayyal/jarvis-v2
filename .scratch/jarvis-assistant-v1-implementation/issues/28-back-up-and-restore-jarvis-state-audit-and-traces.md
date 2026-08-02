# 28 — Back up and restore Jarvis state, audit, and traces

**What to build:** Administrative tooling creates consistent nightly and pre-change Jarvis backups and validates restoration into an isolated path without exposing credentials or changing active services.

**Blocked by:** 27 — Assemble and verify the unactivated deployment bundle.

**Status:** ready-for-agent

- [ ] Backups contain consistent state, append-only audit, deleted-conversation archive, complete diagnostic traces, non-secret configuration, schema, and release metadata.
- [ ] Credential files, private keys, OpenWA state, caches, and external authoritative content remain excluded.
- [ ] Isolated restore verifies checksums, ownership, permissions, database integrity, audit readability, schema compatibility, and release compatibility without activation.

