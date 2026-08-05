# 09 — Authorize terminal actions with deterministic policy

**What to build:** Terminal proposals are authorized through the fixed V1 precedence for hard prohibitions, mandatory-fresh actions, protected resources, exact permissions, provably safe reads, and ordinary approval, with no model classifier.

**Blocked by:** 07 — Freeze and execute one exact approval-gated action.

**Status:** complete

- [ ] Deterministic policy produces the specified refusal, fresh-approval, reusable-permission, safe-read, or ordinary-approval disposition.
- [ ] Every component of a compound command is parsed and authorized before any component can execute.
- [ ] Parser uncertainty, dynamic shell behavior, protected access, or higher-precedence policy can never become implicit authorization.

