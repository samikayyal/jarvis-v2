## Destination

Produce an implementation-ready V1 specification and decision map for a reactive, single-operator WhatsApp assistant that connects to Google services and a Git-backed Obsidian vault, runs policy-controlled commands on Ubuntu and Windows, uses OpenAI for orchestration and Codex as a specialist, with security, approvals, memory, deployment, and verification boundaries fully decided.

## Notes

- This is a planning effort. Stop at an implementation-ready handoff; do not implement the assistant while resolving this map.
- Use the canonical language in [`CONTEXT.md`](../../CONTEXT.md) and consult `/domain-modeling` whenever a term or boundary changes.
- Preserve the completed OpenWA deployment as the messaging gateway. Assistant behavior is a separate layer.
- V1 is reactive, text-only, and controlled by one allowlisted personal WhatsApp number. Scheduled work, proactive monitoring, media understanding, multiple operators, and parallel requests are V2 or later.
- Google is the first account ecosystem: Gmail, Google Calendar, and Google Drive. Reads are allowed; Gmail sends and Calendar changes are approval-gated; destructive Google and Drive actions are excluded.
- The private Git-backed Obsidian knowledge vault is readable. Note writes use an exact approval-gated commit and push; deletion, force-push, history rewriting, and autonomous conflict resolution are excluded.
- Terminal actions may target the always-on Ubuntu host or the authorized operator's personal Windows laptop when available. The orchestration agent selects the host from the natural-language request and known host purpose; Ubuntu is the default, and an unavailable selected host never silently fails over.
- Terminal authorization is entirely deterministic in V1. Model-based command classification, including Gemini, is deferred to V2. Codex is available as a coding and terminal specialist but cannot override authorization policy.
- Working sessions default to 60 minutes and are configurable through deterministic `/config` messages. V1 has one active request and one pending action at a time.
- Natural confirmation messages are parsed deterministically. Pending actions expire after 10 minutes. Selecting a displayed exact-command session or persistent permission creates it immediately; every permission remains narrow, inspectable, and revocable.
- Use `uv` for all Python dependency management and execution.

## Decisions so far

<!-- Resolved tickets are indexed here. -->

- **Choose the agent runtime and Codex boundary** — Jarvis owns session and policy state; the Agents SDK over Responses owns orchestration and traces; Codex runs as a bounded server-side MCP specialist, with an SDK/app-server fallback when stronger interrupt control is required. [Ticket](issues/01-choose-agent-runtime-and-codex-boundary.md) · [Research](research/agent-runtime-and-codex-boundary.md)
- **Define the Google access and OAuth boundary** — Use one External Web application authorization-code flow whose state-bound HTTPS callback is the sole public Jarvis endpoint, with one backend refresh-token record in a private plaintext file owned only by the Google connector; fixed read scopes and incremental `gmail.send` / `calendar.events` write scopes preserve V1 approvals, while Drive mutations and destructive Google actions remain excluded. [Ticket](issues/02-define-google-access-and-oauth-boundary.md) · [Research](research/google-access-and-oauth-boundary.md)
- **Define the OpenWA assistant handoff** — Use a signed `message.received` webhook as the live inbound boundary; authorize by canonical direct-message JID, deduplicate assistant work by `(sessionId, message ID)`, reply via `/messages/reply`, and preserve OpenWA's current direct delivery and transport semantics. [Ticket](issues/03-define-openwa-assistant-handoff.md) · [Research](research/openwa-assistant-handoff.md)
- **Choose the secure two-host execution transport** — Use a private overlay plus outbound mTLS bidirectional gRPC worker sessions; the agent routes natural-language work to default Ubuntu or the personal Windows laptop, an unavailable selected host never fails over, the worker protocol reports readiness/milestones/output and performs process-scope cancellation, and the control listener is overlay-only. [Ticket](issues/04-choose-secure-two-host-execution-transport.md) · [Research](research/secure-two-host-execution-transport.md)
- **Choose the default model and cost policy** — Use explicit `gpt-5.6-terra` with `medium` reasoning on the Agents SDK Responses path; allow canonical Sol/Terra/Luna and all documented GPT-5.6 effort values through deterministic session commands, keep `/config` model defaults persistent for future sessions, and fail closed without silent model substitution when availability or permission checks fail. [Ticket](issues/05-choose-default-model-and-cost-policy.md) · [Research](research/default-model-and-cost-policy.md)
- **Verify the deferred Gemini classifier contract** — `gemini-3.5-flash-lite` research is retained only as V2 background; no model-based classifier participates in V1 terminal authorization. [Ticket](issues/12-verify-gemini-classifier-contract.md) · [Research](research/gemini-classifier-contract.md)
- **Define the state, memory, and audit contract** — Keep Jarvis-owned state local with durable verbatim operator history, explicit memory, request-scoped working content, deterministic lifecycle controls, a permanent redacted audit, and separate full unredacted diagnostic traces—including credential-bearing payloads—retained and backed up indefinitely under manual administration. [Amended ticket](issues/06-define-state-memory-and-audit-contract.md)
- **Define the terminal authorization policy** — Use deterministic precedence across hard prohibitions, three mandatory-fresh classes, protected resources, structurally exact host/path/arguments/cwd permissions that deliberately remain valid across executable-content and inherited-environment changes, provably safe reads, immediate session or persistent grants, deterministic revocation, non-interactive execution, and redacted audit evidence; activation of Jarvis's trust-critical components is manual-only, and model classification remains deferred to V2. [Amended ticket](issues/07-define-terminal-authorization-policy.md)
- **Define the Obsidian sync and write contract** — Use one service-account-owned Ubuntu clone with repository-scoped SSH authentication, deterministic local search, Markdown-only canonical paths, exact base-and-diff approval, a `Jarvis <jarvis@samikayyal.com>` commit identity, normal pushes, and manual conflict recovery. [Ticket](issues/08-define-obsidian-sync-and-write-contract.md)
- **Prototype the WhatsApp control interaction** — Use exact whole-message slash commands and approval phrases around ordinary natural-language requests; make the agent's Ubuntu-default versus personal-Windows-laptop routing decision visible with its reason, expose numbered approval and permission choices, block parallel work, and make milestones, expiry, cancellation, restart, and permission lifetimes visible in the transcript. [Ticket](issues/09-prototype-whatsapp-control-interaction.md) · [Prototype](prototype-09/README.md)
- **Lock the integrated security architecture** — Use a process-isolated deterministic capability broker as the sole authority over admission, policy, approvals, permissions, replay protection, audit gating, and connector dispatch; keep the orchestration agent and untrusted source content non-authoritative, expose only a state-bound Google OAuth callback publicly, isolate service-specific plaintext credentials for unattended restart, bind workers by private-overlay mTLS identity without failover, and require manual activation of trust-critical Jarvis changes. [Ticket](issues/10-lock-integrated-security-architecture.md)
- **Lock the operations and acceptance contract** — Run the Jarvis control plane as an independent, resource-bounded Docker Compose project while preserving OpenWA as its own verified project; use a two-member private Docker handoff network, native host workers, automatic recovery of only the activated release, one validated non-secret config plus isolated credentials, local observability, nightly and pre-change backups, outcome-aware failures, manual pinned upgrades with rollback, and an automated-plus-real acceptance matrix including full-host reboot recovery. [Ticket](issues/11-lock-operations-and-acceptance-contract.md)


## Not yet specified

- Which implementation slices and sequencing should turn the resolved V1
  architecture and acceptance contract into working software.
- Whether live account provisioning exposes additional constraints that cannot be specified from documentation alone.

## Out of scope

- Implementing or deploying the assistant during this Wayfinder effort.
- Scheduled tasks, proactive monitoring, reminders, and assistant-initiated conversations.
- Voice notes, images, documents, and other WhatsApp media processing.
- Multiple authorized operators, group-chat control, and parallel active requests.
- Automatic failover between execution hosts.
- Destructive Google or knowledge-vault actions, force-pushes, history rewriting, and autonomous merge-conflict resolution.
- Public exposure of Jarvis control APIs or direct inbound shell access from the internet.
- Semantic conversation-history retrieval and external embedding indexes in V1.
