Type: task
Status: open
Blocked by: 05

## Question

Implement explicit `o200k_base` `tiktoken` counting over the complete candidate model context, configurable 100,000-token session termination with the fixed operator notice, deterministic WhatsApp reply splitting, configured output bounds, and rotating verbatim JSON Lines runtime traces of all agreed payloads and actions.

Canonicalize instructions, tools, and complete candidate input with the stable
serializer defined by the research contract and terminate at projected count
`>=` the configured limit before every initial or continuation call. Treat the
result as a deterministic local estimate rather than an exact server token
count, and trace returned provider usage for comparison.

Trace failure must produce a deterministic warning without blocking work, and focused boundary tests must avoid model-specific guesswork or silent transcript trimming.

## Comments
