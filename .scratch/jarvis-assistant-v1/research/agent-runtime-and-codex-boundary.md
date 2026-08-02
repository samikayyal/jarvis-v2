# Agent runtime and Codex boundary

Research date: 2026-08-01

## Decision

Jarvis V1 should run as a server-side Python service whose agent runtime is the
OpenAI Agents SDK, using the Responses API path underneath. The Jarvis service,
not the Codex desktop application and not an OpenAI-hosted conversation object,
owns the application-level working session, request lifecycle, authorization,
approval state, host selection, and cancellation policy.

The Agents SDK should own the repetitive agent loop: model turns, typed tool
calls, tool results, handoffs, guardrails, session-history integration, and
tracing. The SDK documentation explicitly distinguishes this managed-runtime
use case from direct Responses API use: direct Responses is appropriate when
the application wants to own the loop, tool dispatch, and state, while the
Agents SDK is appropriate when the runtime should manage turns, tools,
guardrails, handoffs, or sessions. The SDK uses Responses by default for
OpenAI models. See [Agents SDK overview](https://openai.github.io/openai-agents-python/)
and [Agents](https://openai.github.io/openai-agents-python/agents/).

This is a division of responsibility, not a transfer of security authority:
the SDK may pause for an approval or execute a registered function, but only
Jarvis policy code can decide whether an authorized operator may cause a
connected-service mutation or terminal action.

## Ownership contract

| Concern | V1 owner | Required boundary |
| --- | --- | --- |
| Working session | Jarvis session coordinator and durable store | Enforce the 60-minute inactivity boundary, /new, one active request, one pending action, and session-scoped model/reasoning choices. |
| Conversation history | Agents SDK session adapter or equivalent Jarvis-backed history store | History is context, not the source of truth for authorization, approvals, expiry, or durable memory. |
| Agent loop and orchestration | Agents SDK Runner | Run the coordinator and any intentionally scoped specialists; set a finite max-turns limit. |
| Model and reasoning choice | Jarvis configuration, passed through Agent/RunConfig/ModelSettings | Keep model IDs, reasoning effort, verbosity, and tool parallelism in an allowlisted configuration; do not expose arbitrary model selection to the model. |
| Tool schemas and dispatch | Agents SDK function tools plus local handlers | Use typed input/output schemas, validate again in the handler, and preserve call identity and outcomes in the audit record. |
| Side effects | Jarvis policy layer and the concrete connected-service/host adapter | A model tool call is a proposal/request to the application. It is never authorization by itself. |
| Cancellation | Jarvis request controller, then the active SDK/Codex handle | /cancel, session expiry, shutdown, and supersession must mark the request cancelled before stopping work and must suppress late WhatsApp output. |
| Traces | Agents SDK tracing plus a Jarvis audit record | Record workflow/request/session correlation IDs; redact or disable sensitive model/tool payloads according to the data policy. |

## Working sessions and state

The Agents SDK has client-side Sessions that persist conversation history across
agent runs, and it also supports server-managed Responses continuation through
conversation IDs or previous response IDs. These are alternatives, not layers:
the SDK says a Session cannot be combined with conversation_id,
previous_response_id, or auto_previous_response_id in the same run. See
[Agents SDK Sessions](https://openai.github.io/openai-agents-python/sessions/).

For Jarvis, use a Jarvis-backed session record as the canonical lifecycle
object. It should contain at least:

- the Jarvis working-session ID and expiry/last-activity timestamps;
- the selected model and reasoning configuration;
- the active request ID and its cancellation handle;
- the single pending approval-gated action, if any;
- the conversation-history/session adapter reference;
- the Codex specialist thread ID, if one exists for the active request.

An Agents SDK Session implementation can store the conversational history in
that store. Do not make an OpenAI Conversations object the only lifecycle
authority: the official API describes Conversations as durable objects that
can be used across sessions, devices, or jobs, and says conversation items do
not have the normal 30-day Response TTL. That is broader and longer-lived than
Jarvis's explicitly temporary working-session contract. See [Conversation
state](https://developers.openai.com/api/docs/guides/conversation-state).

The service should use Runner.run_streamed for an active request so it can
forward controlled progress, observe tool/approval boundaries, and retain a
live cancellation handle. Set max_turns to a finite V1 value and treat a
limit failure as a controlled request failure, not as permission to continue
indefinitely. The Runner loop is documented as model turn -> tool execution or
handoff -> another model turn until final output or max_turns. See [Running
agents](https://openai.github.io/openai-agents-python/running_agents/).

## Model, reasoning, and structured calls

Use explicit per-session or per-request configuration. The Agents SDK exposes
an agent model and run-level model override, and ModelSettings for reasoning,
verbosity, tool choice, parallel tool calls, truncation, and related controls.
The run-level choice should come from a Jarvis allowlist and may be selected by
deterministic session configuration; the coordinator model must not be allowed
to invent a model ID or silently increase reasoning effort. See [Models and
providers](https://openai.github.io/openai-agents-python/models/) and
[Run configuration](https://openai.github.io/openai-agents-python/running_agents/).

Every Jarvis function tool should have a strict, bounded schema. Prefer
Pydantic-typed arguments and return values, and use the Agents SDK's strict
schema support or the Responses function-tool strict flag. OpenAI's function
calling contract says strict mode makes calls adhere to the schema and
requires additionalProperties=false plus all declared properties marked
required; nullable fields are the way to represent optional values. The
Responses flow returns a function call with a call_id, name, and JSON arguments;
the application executes it and sends a function_call_output carrying the same
call_id. See [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
and [Agents SDK tools](https://openai.github.io/openai-agents-python/tools/).

For V1, set parallel_tool_calls=false for the coordinator. The API otherwise
allows a model to emit multiple function calls in a turn; disabling it gives
Jarvis a simple zero-or-one tool-call boundary that matches its one-active-
request and one-pending-action contract. Re-enable safe read-only parallelism
only as a separately tested policy.

Recommended tool shape:

1. Read-only tools return typed, bounded data and may execute immediately.
2. Mutation tools return a typed proposal first, including the exact target,
   operation, arguments, and expiry. They must not perform the mutation until
   Jarvis's deterministic approval state says that exact proposal is approved.
3. Terminal tools receive structured host, argv, cwd, and purpose fields. The
   handler evaluates the actual command and arguments against deterministic
   terminal policy; a natural-language instruction or model classification
   cannot override a rejection or mandatory approval.
4. The Codex entry point is one narrow specialist tool/handoff, not a general
   shell escape hatch.

Use Agents SDK approval interruptions as a pause/resume mechanism when useful,
but keep the pending-action record and the confirmation parser in Jarvis. The
SDK result surface exposes interruptions and a resumable RunState; that is
useful plumbing, not a replacement for Jarvis's one-action, ten-minute
approval contract. See [Results and state](https://openai.github.io/openai-agents-python/results/)
and [Human in the loop](https://openai.github.io/openai-agents-python/human_in_the_loop/).

## Cancellation

Jarvis should cancel at three layers:

1. Mark the active request cancelled in the Jarvis store and reject any new
   side effect from that request.
2. Call the Agents SDK streaming result's cancel method. The SDK documents
   immediate cancellation and after-turn cancellation; after-turn permits the
   current turn and pending tool work to finish before stopping and saving
   session state. Continue consuming the stream until cleanup completes.
3. Propagate cancellation to the active external operation. For a Codex
   adapter this means interrupting the Codex turn or closing the managed
   specialist process/session, according to the adapter used.

See [RunResultStreaming cancellation](https://openai.github.io/openai-agents-python/results/)
and the first-party [Codex Python SDK API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md),
which exposes TurnHandle.interrupt. If Jarvis ever uses the Responses API's
background mode for a separate long-running job, it must persist the Response
ID and use the documented Responses cancel endpoint; synchronous Responses
cancellation is only connection termination. Background mode is therefore not
the default V1 working-session mechanism. See [Background mode](https://developers.openai.com/api/docs/guides/background).

Cancellation must be idempotent. A cancelled request must not send a final
answer, milestone, approval prompt, or tool result to WhatsApp after the
request's cancellation boundary unless Jarvis deliberately emits a
cancellation acknowledgement.

## Tracing and audit

Enable Agents SDK tracing for each request, with a stable workflow name and
Jarvis session/request IDs as trace or group metadata. The SDK traces the
runner, turns, agents, model generations, function tools, guardrails,
handoffs, and MCP activity. See [Tracing](https://openai.github.io/openai-agents-python/tracing/)
and [MCP tracing](https://openai.github.io/openai-agents-python/mcp/).

Tracing can contain sensitive model and tool inputs/outputs; the SDK documents
that sensitive-data capture is enabled by default. Configure
trace_include_sensitive_data=false or a redacting processor for Jarvis, and
store security-relevant decisions in a separate Jarvis audit record with
operator, session, request, tool, proposal, approval, execution, and outcome
fields. Do not treat an LLM trace as a tamper-resistant authorization log.

## Codex specialist boundary

### Primary integration: Codex MCP behind Agents SDK

The official Codex guidance says to use the Codex SDK for coding-focused
threads, and to run Codex CLI as an MCP server and orchestrate it with the
Agents SDK when Codex is one specialist in a broader workflow. That latter
case matches Jarvis. Start codex mcp-server under an Agents SDK
MCPServerStdio context and expose it only to a dedicated coding specialist
agent or narrow specialist tool. The official integration exposes a codex
tool to start a session and codex-reply to continue it by threadId. See
[Use Codex with the Agents SDK](https://learn.chatgpt.com/docs/mcp-server),
[Agents SDK MCP](https://openai.github.io/openai-agents-python/mcp/), and
[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk).

The adapter contract should be:

- Jarvis creates one Codex specialist thread for the active request when
  needed and stores its thread ID as an implementation detail.
- The model receives a task-specific specialist capability, not raw MCP
  configuration. Host, cwd, model, effort, sandbox, approval policy,
  timeout, and allowed operations come from Jarvis configuration and policy.
- The cwd comes from an allowlisted execution-host registry. The model and
  inbound message cannot select an arbitrary path or silently switch hosts.
- Read-only inspection/review is the default V1 Codex mode. Any
  workspace-write mode requires an already-approved exact Jarvis proposal,
  an allowlisted workspace, and a no-push/no-history-rewrite boundary.
  danger-full-access is excluded.
- Codex's internal command/file approvals must remain visible to and
  consistent with Jarvis's approval boundary. Do not set an internal
  never-approve policy as a way to bypass Jarvis policy for a mutating task.
- Filter the MCP tool surface and convert schemas to strict form where
  possible. The Agents SDK supports static/dynamic MCP tool filters,
  strict-schema conversion, local-server approval policies, and automatic MCP
  trace spans. See [MCP configuration and tool filtering](https://openai.github.io/openai-agents-python/mcp/).
- Normalize the specialist result into a Jarvis-owned typed result such as
  status, summary, changed paths, test evidence, unresolved questions, and
  Codex thread ID. Do not treat Codex's prose as proof that a command or
  mutation succeeded; verify it through the relevant adapter.

The MCP path is the default architectural recommendation because it follows
the official broader-workflow guidance and keeps the main session,
orchestration, approval, and trace model in one Agents SDK runtime.

### Direct Codex SDK/app-server fallback

If V1 requires first-class Codex turn interruption, streamed item inspection,
or typed Codex output that the MCP bridge cannot provide cleanly, isolate that
choice behind the same specialist interface. The first-party Python Codex SDK
is server-side, supports async clients, thread/turn separation, sandbox
presets, output_schema, and TurnHandle.interrupt. Its published package and
API surface are currently documented as beta in the Codex SDK page, so pin and
integration-test it before making it a hard runtime dependency. See [Codex
Python SDK source](https://github.com/openai/codex/tree/main/sdk/python) and
[Python SDK API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md).

The Codex app-server protocol is the lower-level alternative for a deep
product integration: the official docs describe JSON-RPC, streamed agent
events, approvals, and turn/interrupt. It is appropriate only behind a
small adapter if those controls justify the added coupling. See [Codex
App Server](https://learn.chatgpt.com/docs/app-server). The adapter must still
return control to Jarvis for session expiry, authorization, approvals,
host-policy evaluation, cancellation, and final verification.

## Capabilities that must not depend on Codex desktop

The following must work with no Codex desktop window, TUI, IDE extension, or
interactive desktop login:

- WhatsApp message intake, authorized-operator checks, and outbound replies;
- creation, expiry, /new, and persistence of Jarvis working sessions;
- model/reasoning selection and all Agents SDK Runner calls;
- Google/vault/terminal tool registration, schema validation, policy decisions,
  approval prompts, confirmation parsing, and action execution;
- request cancellation, shutdown recovery, timeouts, and suppression of late
  results;
- Codex specialist startup, bounded prompts, host/cwd/sandbox selection,
  thread continuation, approval mediation, result verification, and cleanup;
- traces, security audit records, health checks, deployment, and restart
  recovery.

Codex desktop may remain an optional human-facing surface for manual
development or review. It must not be a daemon, scheduler, credential
broker, session store, approval authority, or required transport for Jarvis.
Use the server-side Codex CLI/MCP path or an explicit Codex SDK/app-server
process instead.

## Confirmed facts, recommendations, and uncertainty

Confirmed by current first-party documentation:

- Agents SDK is a higher-level runtime over Responses for turns, tools,
  handoffs, sessions, and traces.
- Responses function calls use structured function-call items and matching
  call IDs; strict schemas and parallel-call controls are documented.
- Agents SDK streaming results support cancellation, interruptions, and
  resumable state.
- Codex has server-side SDK and CLI/MCP integration paths; official guidance
  specifically describes Codex MCP plus Agents SDK for broader workflows.
- Codex app-server exposes a JSON-RPC integration with streamed events,
  approvals, and turn interruption.

Recommendations for Jarvis, derived from those contracts and the V1 context:

- Make the Jarvis service and its durable session/policy store authoritative.
- Use Agents SDK Runner as the primary loop, with streamed runs, strict typed
  tools, sequential V1 tool calls, finite turns, and redacted traces.
- Use Codex MCP as the default bounded specialist adapter; use the direct
  Python SDK/app-server only when its stronger control surface is required.
- Keep Codex read-only by default and never allow the desktop app to be a
  runtime dependency.

Bounded uncertainty to verify during implementation:

- Exact model IDs, reasoning levels, Codex entitlements, and package versions
  are deployment-time availability concerns; pin and test them rather than
  copying a current documentation example into a permanent product default.
- The Codex MCP bridge's exact interruption/approval round-trip should be
  integration-tested. If a hard per-turn interrupt or approval protocol is
  required, select the documented app-server/SDK adapter instead.
- The Python Codex SDK is currently beta, so its use should remain replaceable
  behind the specialist interface.

## Sources

- [OpenAI Agents SDK overview](https://openai.github.io/openai-agents-python/)
- [Agents SDK sessions](https://openai.github.io/openai-agents-python/sessions/)
- [Agents SDK running agents](https://openai.github.io/openai-agents-python/running_agents/)
- [Agents SDK results](https://openai.github.io/openai-agents-python/results/)
- [Agents SDK tracing](https://openai.github.io/openai-agents-python/tracing/)
- [Agents SDK MCP integration](https://openai.github.io/openai-agents-python/mcp/)
- [OpenAI API function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [OpenAI API conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
- [OpenAI API background mode](https://developers.openai.com/api/docs/guides/background)
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Use Codex with the Agents SDK](https://learn.chatgpt.com/docs/mcp-server)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [First-party Codex Python SDK source](https://github.com/openai/codex/tree/main/sdk/python)
- [First-party Codex Python SDK API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md)
