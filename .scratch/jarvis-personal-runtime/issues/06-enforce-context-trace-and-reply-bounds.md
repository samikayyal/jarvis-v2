Type: task
Status: complete
Blocked by: 05

## Question

Implement explicit `o200k_base` `tiktoken` counting over the complete candidate model context, configurable 100,000-token session termination with the fixed operator notice, deterministic WhatsApp reply splitting, configured output bounds, and rotating verbatim JSON Lines runtime traces of all agreed payloads and actions.

Canonicalize instructions, tools, and complete candidate input with the stable
serializer defined by the research contract and terminate at projected count
`>=` the configured limit before every initial or continuation call. Treat the
result as a deterministic local estimate rather than an exact server token
count, and trace returned provider usage for comparison.

Trace failure must produce a deterministic warning without blocking work, and focused boundary tests must avoid model-specific guesswork or silent transcript trimming.

## Answer

Implemented the deterministic context, output, reply, and trace boundaries for
the personal assistant runtime. Every initial and tool-continuation Responses
candidate is now canonically serialized with sorted compact UTF-8 JSON and
counted through explicit `tiktoken.get_encoding("o200k_base")` with special-
looking text treated as ordinary input. A local estimate at or above the
configured limit returns the fixed context-limit step before a provider call;
the runtime then sends the fixed notice and clears the complete working
session. Provider usage is traced beside the local estimate for comparison.

Configured output-character bounds reject oversized final model text and
prepared-tool results without adding the oversized result to session history.
The existing deterministic safe-boundary WhatsApp splitting remains the single
outbound chunking path.

Added one shared, configurable JSON Lines trace sink for authorized messages,
request lifecycle, OpenAI logical/raw/parsed payloads and retry attempts, tool
calls and results, approval choices, outbound attempts/results, errors, usage,
and timestamps. Rotation occurs only between complete JSON lines and retains
every rotated segment rather than deleting older diagnostic payloads. Trace
write or warning-delivery failures never block work; the runtime emits the
fixed operator warning when trace storage fails.

Focused Issue 06 coverage passed with 64 tests. Repository-wide Ruff, format,
and bytecode compilation passed. The one final full suite completed with 874
passed and 2 skipped; its four failures are the pre-existing immutable legacy
deployment artifact-lock checks reporting that replacement source differs from
the old pinned rollback bundle.

The required two-axis review found no specification gaps. Its one standards
finding (destructive oldest-segment rotation) and duplicated trace-construction
judgment call were repaired and independently rechecked.

## Comments

- Completed on 2026-08-30 after focused TDD, standards/specification review,
  review repair, and the single final full-suite run.
