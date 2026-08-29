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

## Not yet specified

- The exact legacy source, test, deployment, and documentation files that become removable after the replacement passes live acceptance; this sharpens after the replacement seam is inventoried and exercised.
- Exact target-host installation paths, service account, bridge address, Windows SSH identity, and live command choices; these are discovered and recorded during deployment preparation without broadening the architecture.

## Out of scope

- Replacing, upgrading, re-pairing, or redesigning the verified OpenWA messaging gateway.
- Google integrations, vault writes, email, calendar, Drive, scheduling, proactive monitoring, media handling, multiple operators, group control, parallel requests, or request queues.
- Durable assistant memory, searchable conversation history, transcript restoration, context summarization, context compaction, or continuation beyond the hard context limit.
- A general tool-permission language; future mutating tools define their own permission contract when introduced.
- Public assistant or terminal endpoints, automatic host failover, and compatibility or migration for legacy assistant databases, audit records, traces, permissions, or conversation state.
