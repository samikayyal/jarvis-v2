# 15 — Invoke Codex as a bounded, independently verified specialist

**What to build:** Orchestration can invoke Codex only through a closed specialist interface with a frozen execution envelope and independent verification of any approved workspace preparation.

**Blocked by:** 11 — Dispatch bounded work through a controlled worker gateway; 14 — Orchestrate natural-language requests through the Agents SDK.

**Status:** complete

- [x] Every Codex request fixes the host, canonical working directory, model, reasoning, sandbox, approval policy, timeout, and allowed operation.
- [x] Read-only is the default, and workspace preparation requires an already approved exact proposal in an allowlisted workspace.
- [x] Independent verification rejects out-of-scope changes, push, history rewriting, hidden approval bypass, trust-critical activation, and `danger-full-access`.

