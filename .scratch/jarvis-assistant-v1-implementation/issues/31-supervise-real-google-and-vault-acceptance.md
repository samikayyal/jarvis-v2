# 31 — Supervise real Google and knowledge-vault acceptance

**What to build:** Under direct human supervision and exact production approvals, demonstrate the bounded real Google and knowledge-vault behaviors without treating mocks as production proof.

**Blocked by:** 30 — Supervise initial activation and OpenWA handoff acceptance.

**Status:** ready-for-human

- [ ] Real bounded Gmail, Calendar, and Drive reads succeed with the configured identity and fixed scopes, including supervised failure and reconnection cases.
- [ ] One reversibly labeled Gmail send or reply and one reversibly labeled Calendar mutation execute only through exact approval, with altered/replayed/unknown-outcome behavior checked safely.
- [ ] A real deterministic vault read and one exact approved Markdown commit and normal push succeed; excluded and destructive capabilities are tested only through rejection.

