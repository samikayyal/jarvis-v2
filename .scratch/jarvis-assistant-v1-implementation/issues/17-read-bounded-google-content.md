# 17 — Read bounded Gmail, Calendar, and Drive content

**What to build:** Signed operator requests can perform only the approved bounded Gmail, Calendar, and Drive read operations and receive sanitized results through the real orchestration and broker boundaries.

**Blocked by:** 14 — Orchestrate natural-language requests through the Agents SDK; 16 — Complete the state-bound Google OAuth lifecycle.

**Status:** ready-for-agent

- [ ] Each approved read capability enforces fixed operation, result-count, byte, context, identity, and scope bounds.
- [ ] Wrong identity, missing scope, timeout, rate limit, oversized response, and sanitized connector failure produce the specified safe outcomes.
- [ ] Drive mutation, destructive Google operations, generic API access, and credential exposure are absent from the capability surface.

