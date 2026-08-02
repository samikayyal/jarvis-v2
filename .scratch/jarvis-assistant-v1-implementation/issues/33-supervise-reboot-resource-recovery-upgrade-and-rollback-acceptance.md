# 33 — Supervise reboot, resource, recovery, upgrade, and rollback acceptance

**What to build:** Under direct human supervision, run the final disruptive and endurance acceptance matrix proving recovery, resource bounds, degraded modes, backups, upgrades, rollback, and the full-host reboot contract.

**Blocked by:** 31 — Supervise real Google and knowledge-vault acceptance; 32 — Supervise real Ubuntu, Windows, terminal, and Codex acceptance.

**Status:** ready-for-human

- [ ] Automated and supervised endurance, low-disk, trace-capacity, audit-down, resource, and settling checks stay within the specified envelopes and block unsafe work correctly.
- [ ] Backup recovery, a forced failed pinned upgrade, and rollback restore the compatible release and state without touching or replaying OpenWA work.
- [ ] A supervised full-host reboot restores only the activated pinned Jarvis release, returns OpenWA to container `healthy` and session `ready`, preserves persistent state, revokes session authority, does not resume interrupted work, and passes post-reboot messaging.

