Type: research
Status: resolved
Blocked by:

## Question

Given current official OpenAI product and API contracts, what production runtime should own Jarvis's working sessions, tool orchestration, model and reasoning selection, structured tool calls, cancellation, and traces; how should Codex be invoked as a bounded specialist; and which capabilities must not depend on the interactive Codex desktop application?

## Answer

Use a server-side Python service with the OpenAI Agents SDK over the Responses API as Jarvis's primary runtime. Jarvis must own the 60-minute working-session lifecycle, authorization, deterministic approval state, host policy, and cancellation; the SDK owns the agent loop, typed tools, handoffs, and traces. Invoke Codex as a bounded coding specialist through `codex mcp-server` managed by the Agents SDK, with an allowlisted host/cwd, model, sandbox, approval policy, and persisted specialist thread ID. Keep read-only Codex as the default and use the first-party Python Codex SDK/app-server adapter only if direct interrupt, streamed events, or typed output are required. The desktop Codex application must remain optional and must not own sessions, orchestration, approvals, credentials, cancellation, deployment, or WhatsApp behavior. See the [research artifact](../research/agent-runtime-and-codex-boundary.md).

## Comments
