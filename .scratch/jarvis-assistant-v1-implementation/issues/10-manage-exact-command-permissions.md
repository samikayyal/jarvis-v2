# 10 — Create, match, list, and revoke exact command permissions

**What to build:** Eligible terminal approvals can create narrow working-session or persistent permissions whose exact matching rules are inspectable and immediately revocable without bypassing higher policy.

**Blocked by:** 09 — Authorize terminal actions with deterministic policy.

**Status:** complete

- [ ] Permission identity binds host, resolved executable or script path, full arguments, canonical working directory, and normalized compound structure.
- [ ] Session and persistent permission choices create and execute atomically with the defined lifetimes and provenance.
- [ ] Listing is deterministic, revocation removes usable authority before acknowledgement, and higher policy always takes precedence over a match.
