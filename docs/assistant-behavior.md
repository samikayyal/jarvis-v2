# Assistant behavior and runtime context

The editable `SYSTEM.md` defines identity, initiative, scope, evidence standards,
and communication. `deployment/personal-runtime/SYSTEM.md.example` is the reviewed
starting point. Updating the example does not change an installed prompt; deploy
the prompt explicitly and preserve the old prompt alongside the previous release
for rollback. Restart loads the new prompt and clears the in-memory session.

For each ordinary request, `PersonalRuntime` appends an allowlisted runtime context:
active model/reasoning, OpenAI remote inference, WhatsApp/OpenWA channel, request
time in the operator's timezone, process host/OS, relevant configured paths, and
limits. It contains no credential values or operator/chat identifiers. `/model`
and `/reasoning` changes appear in the next ordinary request. Approval continuations
retain the originating request's settings and timestamp. `/status` counts the same
context structure along with the tool schemas and retained conversation.

These facts describe configuration and process identity, not hardware utilization,
gateway health, or vendor pricing. Relevant live observations still require tools.
Skills own domain procedures; retrieved notes cannot replace current runtime facts
or override approval rules. Hosting comparisons hold inference constant as their
baseline and explicitly separate any proposed provider migration.

When the next tool call would exceed the request limit, the runtime records a
`not_attempted` result and requests one summary with no tools and `tool_choice=none`.
The existing request deadline still bounds this attempt. It returns an `incomplete`
disposition with the exact limit notice and a partial account of verified results
and remaining work. If summary generation fails, a deterministic fallback preserves
the distinction between incomplete work and completed actions. Follow-ups retain
prior results and a valid call/result transcript; they do not replay earlier actions.
Timeout replies identify the deadline and possible uncertain outcomes. Errors on
approval continuations receive the same trace coverage as initial requests.

Final model prose is converted for WhatsApp: headings and bold emphasis use
WhatsApp syntax, links retain their URLs, and simple Markdown tables become labeled
bullets. Inline and fenced code contents are preserved. Frozen approval previews,
commands, and stored note content bypass this conversion. Message splitting prefers
paragraphs and then lines; outbound chunks still have one-attempt delivery semantics.

Intermediate progress messages are intentionally excluded. Existing typing presence
and final/approval messages are unchanged in timing. No provider or permission change
is part of this redesign.

## Acceptance cases

- Identity: names itself Jarvis and reports the active model from runtime context.
- Hosting: treats moving Jarvis to GCP as application/gateway hosting, with existing
  remote inference as the baseline; no invented Gemini dependency or GPU requirement.
- Evidence: identifies an unavailable VM price specifically instead of substituting
  token pricing; distinguishes estimates from observed measurements.
- Model override: next request reports the new active model and reasoning.
- Limit after approval: completed action remains recorded, further tool is not run,
  partial answer is labeled incomplete, next request has valid history.
- Transport: prose is readable in WhatsApp; approval previews remain byte-for-byte
  unchanged before existing chunk splitting; no progress-message delivery is added.
