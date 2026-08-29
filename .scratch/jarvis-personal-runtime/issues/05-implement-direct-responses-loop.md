Type: task
Status: open
Blocked by: 01, 03

## Question

Implement the direct OpenAI Responses model-and-tool loop using the researched current contract: sequential tool execution, no parallel tool calling, current-session transcript ownership, session model and reasoning overrides, configured round and request limits, complete traceable payloads, tool-error continuation, cancellation, and a final text result.

Pin the OpenAI SDK behind a narrow raw-response adapter, resend instructions on
every call, preserve every output item needed by stateless reasoning, enforce
`store=False` and `truncation="disabled"`, wrap normal SDK retries in the overall
request deadline, trace every HTTP attempt through the transport, and describe
foreground cancellation only as local best effort.

Do not use the Agents SDK or introduce a broker, generalized connector layer, provider-owned durable conversation, or automatic model fallback.

## Comments
