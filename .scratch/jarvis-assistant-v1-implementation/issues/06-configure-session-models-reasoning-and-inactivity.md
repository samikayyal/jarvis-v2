# 06 — Configure session models, reasoning, and inactivity

**What to build:** The operator can deterministically select only canonical model, reasoning, and inactivity values while keeping session choices separate from persistent defaults and preventing silent substitution.

**Blocked by:** 05 — Manage working sessions, `/status`, `/cancel`, and `/new`.

**Status:** complete

- [ ] `/model`, `/reasoning`, and `/config` accept only canonical values and enforce the allowed mutation timing.
- [ ] Session choices and future-session defaults have distinct, correctly persisted lifetimes.
- [ ] Inactivity pauses during genuine processing, resumes while idle, and unavailable model or reasoning choices fail closed without substitution.
