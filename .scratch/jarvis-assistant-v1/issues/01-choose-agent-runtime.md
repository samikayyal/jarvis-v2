Type: research
Status: resolved
Blocked by:

## Question

Given current official OpenAI product and API contracts, what production runtime should own Jarvis's working sessions, tool orchestration, model and reasoning selection, structured tool calls, cancellation, and traces?

## Answer

Use a server-side Python service with the OpenAI Agents SDK over the Responses API as Jarvis's primary runtime. Jarvis owns the 60-minute working-session lifecycle, authorization, deterministic approval state, host policy, cancellation, and durable state. The SDK owns the agent loop, typed tools, handoffs, model execution, and traces. See the [research artifact](../research/agent-runtime.md).

## Comments
