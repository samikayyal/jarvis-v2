# Agent runtime boundary

Research date: 2026-08-01

## Decision

Jarvis V1 runs as a server-side Python service using the OpenAI Agents SDK over the Responses API. Jarvis, rather than an OpenAI-hosted conversation object, owns the working session, request lifecycle, authorization, approval state, host selection, cancellation policy, and durable state.

The Agents SDK owns model turns, typed tool calls and results, guardrails, session-history integration, and tracing. This division does not transfer security authority: only deterministic Jarvis policy may authorize a connected-service mutation or terminal action.

## Ownership contract

| Concern | V1 owner | Required boundary |
| --- | --- | --- |
| Working session | Jarvis session coordinator and durable store | Enforce inactivity, `/new`, one active request, one pending action, and session-scoped model/reasoning choices. |
| Conversation history | Jarvis-backed history store | History is context, not authority for approvals, expiry, or durable memory. |
| Agent loop and orchestration | Agents SDK Runner | Use finite turn and tool limits with sequential tool calls. |
| Model and reasoning choice | Jarvis configuration | Allowlist model IDs and reasoning effort; never permit silent substitution. |
| Tool schemas | Agents SDK function tools plus local handlers | Use strict bounded schemas and validate again in each handler. |
| Side effects | Jarvis policy and capability adapters | A model tool call is a proposal, never authorization. |
| Cancellation | Jarvis request controller, then the active SDK task | Mark cancellation before stopping work and suppress late output. |
| Traces | Jarvis diagnostic trace boundary | Correlate session and request IDs while keeping security audit separate. |

## Runtime rules

- Use a Jarvis-backed session record as the canonical lifecycle object.
- Use explicit per-session model and reasoning configuration.
- Set `parallel_tool_calls=false` for the V1 coordinator.
- Read-only tools return typed bounded data; mutation and terminal tools produce proposals and cannot dispatch directly.
- Keep pending-action state and confirmation parsing deterministic and Jarvis-owned.
- Cancellation is idempotent and waits for model-task quiescence before completing.
- Model and tool content is untrusted input and cannot establish authority.

## Sources

- [Agents SDK overview](https://openai.github.io/openai-agents-python/)
- [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
