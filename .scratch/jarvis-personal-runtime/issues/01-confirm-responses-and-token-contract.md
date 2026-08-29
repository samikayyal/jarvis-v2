Type: research
Status: resolved
Blocked by:

## Question

Against current official OpenAI and `tiktoken` primary sources, what exact Python API contract should the replacement use for a direct sequential Responses function-tool loop with `store=false`, model reasoning configuration, parallel tool calls disabled, complete request/response tracing, tool-output continuation, cancellation and retry behavior, and deterministic `o200k_base` session token counting?

Record the answer in [`../research/responses-and-token-contract.md`](../research/responses-and-token-contract.md), including version-sensitive caveats that implementation and tests must pin.

## Answer

Use the pinned official Python Responses client directly with `store=False`,
`parallel_tool_calls=False`, `truncation="disabled"`, an explicit model and
reasoning effort, repeated system instructions, and the complete API-shaped
in-memory input on every turn. After a function call, replay every response
output item—including opaque encrypted reasoning items—then append one
`function_call_output` with the exact `call_id`. Reject a provider response
that violates the zero-or-one-call invariant.

Keep the SDK's documented retry policy inside a Jarvis-owned overall deadline.
Foreground `/cancel` cancels only the local async wait and is best effort at the
provider boundary. Pin the SDK behind a small raw-response adapter; trace every
HTTP attempt through transport instrumentation so automatic retry responses are
not omitted from the agreed verbatim trace.

Apply the context gate before every initial or continuation request by encoding
a stable canonical JSON representation of instructions, tools, and complete
candidate input with `tiktoken.get_encoding("o200k_base")`. A projected count
`>= 100_000` (or the configured value) ends the session before the API call.
This is Jarvis's deterministic local budget, not an exact prediction of the
server's structured input count; log returned usage for comparison during
acceptance.

Full evidence, examples, implementation checks, caveats, and primary-source
citations are in the [research artifact](../research/responses-and-token-contract.md).

## Comments

- Claimed by the background research session on 2026-08-29; findings are being prepared on `research/responses-token-contract` in an isolated worktree.
- Resolved on 2026-08-30 after integrating and verifying research commit `de530eb`.
