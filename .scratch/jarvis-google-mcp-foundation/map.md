## Destination

Add the smallest reusable configured-MCP-service support to the native Jarvis
personal assistant runtime, use it to connect one Google connection to bounded
Gmail, Google Drive, and Google Calendar capabilities, pass focused and real
supervised acceptance, and activate the integration without regressing the
existing WhatsApp, vault, or terminal paths.

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
  `stdio` transport or its server-lifecycle concerns.
- Prefer Google's official Workspace MCP servers. Their Developer Preview
  status is an explicit supervised activation risk, not a reason to adopt an
  unofficial all-in-one server.
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
  Keep the OAuth client secret and refresh token only in `.env`, and keep all
  OAuth material out of model context and runtime traces. Jarvis never edits
  `.env`; any required writable token store needs an explicit domain amendment.
- Use the existing runtime trace path for bounded MCP activity while excluding
  OAuth tokens and authorization headers. Do not build a separate redaction or
  security subsystem.
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
  OAuth-protected Streamable HTTP tools contract that Jarvis can snapshot and
  fail closed against, but Google publishes no production support or immutable
  version contract for them. Calendar supports approved create/update; Gmail
  currently supports search/read and draft creation but no send/reply, so the
  original Gmail-write acceptance remains blocked pending an explicit map
  amendment or a suitable official operation.
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
  has no send/reply operation.

## Not yet specified

- Production authorization and reconnect cannot be locked while Google
  publishes no support/version contract for the hosted endpoints and Gmail
  exposes no send/reply operation; Ticket 02 may prototype the verified OAuth
  shape but cannot erase those activation blockers.
- The exact target-host configuration values, credential paths, service
  restart procedure, and rollback commands depend on the selected official
  server deployment shape and the implemented runtime seam.
- The exact real Gmail, Drive, and Calendar acceptance fixtures will be chosen
  during supervised activation without placing private content in the repo.

## Out of scope

- Dynamic trust of discovered MCP tools, arbitrary user-supplied MCP servers,
  an MCP marketplace, or a general plugin-management interface.
- Local `stdio` MCP servers and lifecycle management of local server processes.
- Multiple Google identities, account switching, domain-wide delegation, or
  unattended service-account access to operator data.
- Drive mutation or sharing, Gmail label/archive/trash/delete operations,
  Calendar deletion, account administration, and destructive Google actions.
- Persistent approval for Google writes, automatic retry of ambiguous writes,
  and automatic failover to alternate MCP servers or direct Google APIs.
- Reintroducing the retired control plane, capability broker, durable proposal
  store, connector framework, audit subsystem, or public Jarvis OAuth callback
  unless official Google requirements later make that callback unavoidable and
  the map is explicitly amended.
- Implementing integrations for services other than Google in this effort.
