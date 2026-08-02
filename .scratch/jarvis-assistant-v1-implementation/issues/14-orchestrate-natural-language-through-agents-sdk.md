# 14 — Orchestrate natural-language requests through the Agents SDK

**What to build:** An admitted natural-language request can use the configured Agents SDK over Responses to select bounded reads or typed proposals, choose and explain an execution host, and produce milestone and final outcomes without acquiring authority.

**Blocked by:** 04 — Retain complete diagnostic traces and enforce trace capacity; 06 — Configure session models, reasoning, and inactivity; 07 — Freeze and execute one exact approval-gated action.

**Status:** ready-for-agent

- [ ] The orchestration adapter uses explicit configured model/reasoning values, bounded sequential tools, and provider-side conversation persistence disabled.
- [ ] Host-neutral terminal work defaults to Ubuntu, Windows-dependent work selects Windows with a visible reason, and unavailable hosts never trigger failover.
- [ ] Malformed tool output, prompt injection, model unavailability, and model-proposed authority changes fail closed at the broker boundary.

