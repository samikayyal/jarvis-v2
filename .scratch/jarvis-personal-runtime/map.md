## Destination

Actually replace the existing Jarvis assistant control plane with a small native Ubuntu personal assistant runtime that preserves the verified OpenWA gateway, implements the agreed deterministic command and direct OpenAI tool loop, passes real Ubuntu, Windows, and WhatsApp acceptance, and then retires the obsolete runtime.

## Notes

- This is an execution-bearing Wayfinder effort. Tickets may implement, test, install, cut over, and remove code; the map is complete only when the replacement is live and the legacy assistant runtime is retired.
- Preserve the existing OpenWA deployment, dedicated WhatsApp account, Baileys pairing state, and readiness contract. Redesign only the assistant side of the handoff.
- Use the canonical language in [`CONTEXT.md`](../../CONTEXT.md) and consult `/domain-modeling` whenever a term or boundary changes.
- Accept only direct text from the configured authorized WhatsApp number. Ignore other senders, groups, self-authored traffic, and media. Retain only a seven-day on-disk OpenWA message-ID deduplication cache.
- Run one native Python service under `systemd` on Ubuntu. OpenWA reaches its non-public listener through the Docker host bridge. Ubuntu terminal commands use local subprocesses; Windows commands use OpenSSH over Tailscale without custom workers.
- Handle every slash-prefixed message deterministically. Initial commands are `/help`, `/new`, `/status`, `/cancel`, `/model`, `/reasoning`, `/permissions`, and `/forget-permission`.
- Send ordinary text directly through a sequential OpenAI Responses tool loop with parallel tool calling disabled. Initial prepared tools are `read_vault` and `run_terminal` only.
- Use `.env` for secrets, `jarvis.toml` for non-secret configuration and saved permissions, and `SYSTEM.md` for the editable system prompt. Jarvis never edits `.env` and writes only the saved-permissions section of TOML.
- Run configured simple read-only commands automatically. Pipes, redirects, substitutions, logical operators, separators, scripts, and unmatched commands require approval unless a saved host-plus-literal-prefix permission matches.
- One pending action waits indefinitely. After trimming surrounding whitespace, only exact `1`, `2`, `9`, and `/cancel` are recognized; all other messages are silently ignored. `1` approves once, `2` approves and saves the proposed host-plus-command-prefix rule, and `9` rejects.
- Sessions live only in memory and end on `/new`, configured inactivity, restart, or the configurable 100,000-token limit counted with explicit `tiktoken` encoding `o200k_base`. Context overflow sends the fixed notice and does not process the triggering message.
- Split long WhatsApp replies deterministically at safe text boundaries. Use configured tool-round, request, command, and output limits. Never automatically rerun terminal commands or future mutating tools.
- Record rotating verbatim JSON Lines runtime traces containing authorized messages, OpenAI request and response payloads, tool calls and results, terminal activity, approvals, errors, and timing. Trace failure warns but does not block work. Hidden model reasoning is unavailable and is not part of the trace.
- Keep implementation narrow. Add no capability broker, Agents SDK, generalized connector framework, specialized workers, mTLS, authorization audit, durable memory, conversation archive, recovery engine, or compatibility layer for the old control-plane state.
- Use `uv` for all Python dependency management and execution. Run focused tests while developing and the full surviving suite only once, at the end.
- Live cutover remains human-supervised for `.env`, service installation/replacement approval, mutating smoke-test approvals, phone receipt, and authorization to retire the previous runtime.

## Decisions so far

<!-- Resolved tickets are indexed here. -->

- [Confirm the direct Responses and token-counting contract](issues/01-confirm-responses-and-token-contract.md) — Use a pinned direct stateless Responses loop that replays complete output items, locally cancels foreground work, traces every HTTP attempt, and gates each candidate request with a deterministic canonical `o200k_base` estimate rather than claiming an exact server token count.
- [Inventory the replacement seam](issues/02-inventory-replacement-seam.md) — Add a wholly independent `jarvis_personal_runtime` package, retain the immutable legacy runtime only for rollback until live acceptance, preserve OpenWA and its pairing unchanged, reimplement only revalidated gateway facts, and defer removal of the complete old source/test/deployment boundary to the separately authorized retirement ticket.
- [Build the runtime foundation](issues/03-build-runtime-foundation.md) — Use one fresh admitted-text runtime seam with strict three-file configuration loading, in-memory session/request/approval state, deterministic commands and local cancellation, section-only saved permissions, and an atomic seven-day message-ID cache; keep OpenWA, Responses, and terminal adapters in later tickets.
- [Connect the OpenWA message flow](issues/04-connect-openwa-message-flow.md) — Authenticate and promptly acknowledge the exact direct-text handoff, exclude unauthorized or unsupported traffic before runtime work, rely on the replacement seven-day message-ID cache, and send deterministic one-attempt chunks through the preserved OpenWA `send-text` API without blocking later acknowledgements.
- [Implement the direct Responses loop](issues/05-implement-direct-responses-loop.md) — Pin the direct OpenAI SDK behind a raw-response adapter, own and replay the complete in-memory working-session transcript, execute one prepared tool per round, contain normal retries within the configured request deadline, trace every HTTP attempt and complete payload, and treat foreground cancellation only as local best effort.
- [Enforce context, trace, and reply bounds](issues/06-enforce-context-trace-and-reply-bounds.md) — Gate every complete candidate context with the stable explicit `o200k_base` estimate, end the session at `>=` the configured limit, enforce configured output bounds and deterministic WhatsApp splitting, and retain rotating verbatim JSON Lines trace segments with non-blocking deterministic failure warnings.
- [Add the read-vault tool](issues/07-add-read-vault-tool.md) — Expose one strict `read_vault` prepared tool with minimal `mode` and `value` inputs, exact safe Markdown reads, deterministic bounded local search, configured vault rooting, and rejection of traversal, writes, unsupported targets, and oversized data.
- [Run approved Ubuntu commands](issues/08-run-approved-ubuntu-commands.md) — Expose `run_terminal` for the native Ubuntu host with strict simple-command read-only screening, exact host-plus-prefix approvals and saved permissions, resumable tool results, configured cwd/deadline/output limits, single-attempt execution, and best-effort process-group cancellation.
- [Package the native service and private handoff](issues/10-package-native-service-and-private-handoff.md) — Ship one checksum-pinned native service template and bounded private webhook listener, keep target-host values deployment-discovered, and document inactive validation, installation, operation, and rollback without touching live OpenWA or the previous runtime.
- [Prove the replacement contract](issues/11-prove-replacement-contract.md) — The complete focused replacement-runtime contract passes on the development host (139 passed, one expected POSIX-only skip), with no runtime-behavior defect found and no activation, removal, or repository-wide test run performed.
- [Activate and accept the live replacement](issues/12-activate-and-accept-live-replacement.md) — The checksum-pinned native runtime is live on the private OpenWA handoff; real WhatsApp/OpenAI, Ubuntu, Windows, approval, rejection, cancellation, trace, readiness, pairing, phone-receipt, review, and final go/no-go gates passed while the stopped legacy runtime remains preserved for Ticket 13 retirement.

## Not yet specified

- The exact legacy source, test, deployment, and documentation files that become removable after the replacement passes live acceptance; this sharpens after the replacement seam is inventoried and exercised.
- Exact target-host installation paths, service account, bridge address, Windows SSH identity, and live command choices; these are discovered and recorded during deployment preparation without broadening the architecture.

## Out of scope

- Replacing, upgrading, re-pairing, or redesigning the verified OpenWA messaging gateway.
- Google integrations, vault writes, email, calendar, Drive, scheduling, proactive monitoring, media handling, multiple operators, group control, parallel requests, or request queues.
- Durable assistant memory, searchable conversation history, transcript restoration, context summarization, context compaction, or continuation beyond the hard context limit.
- A general tool-permission language; future mutating tools define their own permission contract when introduced.
- Public assistant or terminal endpoints, automatic host failover, and compatibility or migration for legacy assistant databases, audit records, traces, permissions, or conversation state.
