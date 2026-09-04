## Destination

Add the smallest reusable configured-MCP-service support to the native Jarvis
personal assistant runtime as a generic capability. Use bounded Google API
tools backed by Google's generally available official Gmail, Drive, and
Calendar REST APIs for the exact personal account
`kayyal.sami@gmail.com`. Pass focused and real supervised acceptance, and
activate the integration without regressing the existing WhatsApp, vault, or
terminal paths.

## Notes

- This is an execution-bearing Wayfinder effort. Tickets may research, decide,
  prototype, implement, test, configure, and activate. Live Google consent,
  credential handling, production installation, real sends/calendar changes,
  and final go/no-go remain human-supervised.
- Keep the implementation as simple as possible. Do not add a capability
  broker, connector framework, plugin marketplace, durable authorization or
  audit database, generalized permission language, service-management UI, or
  compatibility layer for the retired control plane.
- Use the canonical language in [`CONTEXT.md`](../../CONTEXT.md) and consult
  `/domain-modeling` whenever a term or boundary changes. A configured MCP
  service is not a dynamically trusted plugin: one checked-in operation
  manifest selects which server operations Jarvis exposes as prepared tools.
- Use remote Streamable HTTP for configured MCP services. Do not add a local
  `stdio` transport or its server-lifecycle concerns. This generic MCP
  capability is retained, but it is not the active route for personal Google.
- Use Google's generally available official Gmail, Drive, and Calendar REST APIs
  for the active personal Google route. Do not require Workspace Developer
  Preview enrollment or route `kayyal.sami@gmail.com` through Google's hosted
  preview MCP endpoints.
- Maintain one Google connection. Reauthorization or disconnection invalidates
  pending Google actions created under the prior connection.
- Initial reads are bounded Gmail search/read, Drive search/metadata/text
  content/export, and Calendar search/read. Gmail send/reply and Calendar
  create/update require one exact approval. Drive mutations, destructive
  actions, arbitrary Google endpoints, and arbitrary discovered MCP tools are
  excluded.
- Reuse the existing approval grammar: `1` approves the exact action once; `9`
  and `/cancel` reject it. `2` remains terminal-only and never saves Google
  write permission. Ambiguous writes are not retried automatically.
- Add only `/connections`, `/connect google`, and `/disconnect google` as
  deterministic connection controls. Do not add generic conversational MCP
  configuration.
- Store non-secret server configuration and manifest paths in `jarvis.toml`.
  Store the exact Google account in the `[google]` section. Keep the OAuth
  client secret and refresh token only in `.env`, and keep all OAuth material
  out of model context and runtime traces. Jarvis never edits `.env`; any
  required writable token store needs an explicit domain amendment.
- Use the existing runtime trace path for bounded Google API and configured MCP
  activity while excluding OAuth tokens and authorization headers. Do not build
  a separate redaction or security subsystem.
- Later services should normally require only pinned service configuration, an
  explicit operation manifest, a small prepared-tool adapter, contract tests,
  and supervised acceptance. A genuinely new approval or lifecycle primitive
  may justify a core-runtime change.
- Use `uv` for all Python dependency management and execution. Run focused
  tests during development and the full surviving suite only once at the end.

## Decisions so far

<!-- Resolved tickets are indexed here. -->

- [Confirm the official Google Workspace MCP contract](issues/01-confirm-official-google-workspace-mcp-contract.md)
  — Google's three service-specific hosted endpoints expose a stateless,
  OAuth-protected Streamable HTTP tools contract that the generic configured MCP
  capability can snapshot and fail closed against, but Google publishes no
  production support or immutable version contract for them. Calendar supports
  approved create/update; Gmail currently supports search/read and draft
  creation but no send/reply. They are not the active route for the personal
  Google account.
- [Lock the minimal configured MCP service boundary](issues/02-lock-the-minimal-configured-mcp-service-boundary.md)
  — A tested `ConfiguredMcpService` prototype now fails closed against exact
  selected discovery, exposes only narrowed manifest operations, binds one
  opaque Google connection, and reuses pending approvals while keeping `2` and
  saved permissions terminal-only; HTTP/OAuth and config-parser wiring remain
  with Ticket 03.
- [Implement Google through the minimal MCP foundation](issues/03-implement-google-through-the-minimal-mcp-foundation.md)
  — The native runtime now loads explicit remote services, validates checked-in
  digest manifests against live discovery, binds one OAuth-backed Google
  connection, exposes 10 bounded reads and two exact one-attempt Calendar
  writes, and keeps Gmail read-only because the official hosted service still
  has no send/reply operation. This implementation remains available as the
  generic configured MCP capability, not as the active personal Google route.
- [Prove the complete Google MCP contract](issues/04-prove-the-complete-google-mcp-contract.md)
  — Focused contract and regression gates, static checks, and the one final
  repository-wide suite prove the supportable official Google contract without
  live authorization or production changes; the final suite passed with 172
  tests and one existing platform-conditional skip. Those gates cover the
  retained generic MCP capability.
- [Supervise Google authorization and activation](issues/05-supervise-google-mcp-authorization-and-activation.md)
  — The exact personal account is active through bounded official Gmail, Drive,
  and Calendar REST APIs after supervised OAuth, live reads and writes,
  excluded-mutation checks, reconnect and restart invalidation, unchanged-path
  acceptance, production reconciliation, and the operator's explicit go.
- Personal Google route amendment (2026-09-03) — The exact authorized identity
  `kayyal.sami@gmail.com` cannot enroll in Google's Workspace Developer Preview
  Program, and Google's hosted Gmail MCP has no send/reply operation. The
  active route is therefore bounded Google API tools backed by the generally
  available official Gmail, Drive, and Calendar REST APIs. The generic
  `ConfiguredMcpService` capability remains separate and is not used for this
  Google connection.

## Not yet specified

- No remaining item in this effort. Any expansion beyond the activated fixed
  personal Google tool set requires a separate decision.

## Out of scope

- Dynamic trust of discovered MCP tools, arbitrary user-supplied MCP servers,
  an MCP marketplace, or a general plugin-management interface.
- Local `stdio` MCP servers and lifecycle management of local server processes.
- Multiple Google identities, account switching, domain-wide delegation, or
  unattended service-account access to operator data.
- Drive mutation or sharing, Gmail label/archive/trash/delete operations,
  Calendar deletion, account administration, and destructive Google actions.
- Persistent approval for Google writes, automatic retry of ambiguous writes,
  and automatic failover between alternate Google routes or services.
- Reintroducing the retired control plane, capability broker, durable proposal
  store, connector framework, audit subsystem, or public Jarvis OAuth callback
  unless official Google requirements later make that callback unavoidable and
  the map is explicitly amended.
- Implementing integrations for services other than Google in this effort.
