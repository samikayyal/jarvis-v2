Type: task
Status: complete
Blocked by: 01, 03

## Question

Implement the direct OpenAI Responses model-and-tool loop using the researched current contract: sequential tool execution, no parallel tool calling, current-session transcript ownership, session model and reasoning overrides, configured round and request limits, complete traceable payloads, tool-error continuation, cancellation, and a final text result.

Pin the OpenAI SDK behind a narrow raw-response adapter, resend instructions on
every call, preserve every output item needed by stateless reasoning, enforce
`store=False` and `truncation="disabled"`, wrap normal SDK retries in the overall
request deadline, trace every HTTP attempt through the transport, and describe
foreground cancellation only as local best effort.

Do not use the Agents SDK or introduce a broker, generalized connector layer, provider-owned durable conversation, or automatic model fallback.

## Answer

Implemented the direct stateless OpenAI Responses model-and-tool loop behind a
pinned `openai==2.53.0` raw-response adapter. The runtime owns the complete
API-shaped working-session transcript, resends system instructions and current
session model/reasoning selections on every request, executes at most one
prepared tool per round, preserves opaque reasoning and all response output
items, returns deterministic tool errors to the model, and requires final text.

Every request explicitly sets `store=False`, `parallel_tool_calls=False`, and
`truncation="disabled"`. The configured overall deadline contains the SDK's
normal retry/backoff behavior and passes each call its remaining time; transport
hooks trace every HTTP attempt without credential headers, while the adapter
also traces the raw final exchange and complete parsed Response. Foreground
cancellation is recorded only as local best effort, never as confirmed provider
cancellation.

The existing runtime now starts a fresh runner transcript with each fresh
working session while remaining compatible with structural test runners that do
not own session state. Focused Responses/runtime/OpenWA tests passed (33), as did
repository-wide Ruff, formatting, and bytecode compilation. The one final full
suite completed with 861 passed and 2 skipped; the four surviving failures are
the expected immutable legacy deployment artifact-lock checks reporting that
the new replacement source differs from the old pinned rollback bundle.

## Comments

- Completed on 2026-08-30 after the required standards and specification
  reviews.
