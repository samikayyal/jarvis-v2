Type: research
Status: resolved
Blocked by:

## Question

What are the current authoritative server topology, Streamable HTTP endpoints,
authentication and OAuth flow, operation names and schemas, Developer Preview
constraints, deployment expectations, and version-pinning options for Google's
official Gmail, Drive, and Calendar MCP services, and which facts constrain the
smallest production-capable Jarvis integration?

## Answer

### Decision

The map's original premise is only partly supportable as of 2026-09-02.

Google operates three live, service-specific Streamable HTTP endpoints:

| Service | Exact endpoint | Live `tools/list` count |
| --- | --- | ---: |
| Gmail | `https://gmailmcp.googleapis.com/mcp/v1` | 23 |
| Drive | `https://drivemcp.googleapis.com/mcp/v1` | 8 |
| Calendar | `https://calendarmcp.googleapis.com/mcp/v1` | 9 |

They are demonstrably Google-hosted protocol services. The Google-maintained
[`google/mcp` catalog repository][google-mcp-catalog] lists Workspace under
**open-source MCP servers**, linking the
[Google Workspace extension for Gemini CLI][workspace-extension], rather than
listing these endpoints among its examples of managed remote servers. However,
that list says it contains only "a few key" remote servers and the repository
disclaims official product support, so its omission proves nothing about the
hosted services' status. No Google-owned public service page establishing their
support, release, Developer Preview, or immutable-version contract was located
in this research. Their provenance and production support therefore remain
unconfirmed rather than disproved.

There is also a functional blocker: the live Gmail endpoint exposes no send
operation. It can search/read mail and `create_draft`, including a reply draft
via `replyToMessageId`, but it cannot send that draft or send/reply directly.
Therefore the later tickets cannot meet the map's Gmail send/reply acceptance
through the selected official remote-only boundary. Do not silently substitute
the direct Gmail API or an unofficial server. Ticket 02 must either reduce the
first release to Gmail reads plus draft creation, or explicitly amend the
effort's boundary before implementation.

### Reproducible live protocol contract

The following unauthenticated JSON-RPC requests were run directly against each
endpoint on 2026-09-02:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"jarvis-contract-probe","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

After the successful `initialize` response, the notification was sent before
normal operations and returned HTTP 202 from each endpoint. Both subsequent
POSTs explicitly carried the negotiated `MCP-Protocol-Version: 2025-06-18` HTTP
header. The response digests below were reconfirmed with this lifecycle sequence.

Each `initialize` response selected MCP `2025-06-18`, identified the server as
`StatelessServer` version `ESF`, and advertised only tools with
`listChanged: false`. Responses were ordinary `application/json`; no
`Mcp-Session-Id` was issued. `GET` on the exact endpoints returned 405, while
`POST` handled `initialize` and `tools/list`. This is a stateless POST-only
subset of the [MCP Streamable HTTP transport][mcp-streamable-http], not a local
`stdio` topology and not three processes Jarvis must deploy.

`tools/list` is public, but `tools/call` is not. An unauthenticated tool call
returned JSON-RPC `isError: true`, HTTP 401, and a `WWW-Authenticate: Bearer`
challenge pointing to operation-specific RFC 9728 protected-resource metadata.
The service-wide metadata is available at:

- `https://gmailmcp.googleapis.com/.well-known/oauth-protected-resource/mcp/v1`
- `https://drivemcp.googleapis.com/.well-known/oauth-protected-resource/mcp/v1`
- `https://calendarmcp.googleapis.com/.well-known/oauth-protected-resource/mcp/v1`

All three name `https://accounts.google.com/` as the authorization server and
support bearer tokens in the `Authorization` header. The
[Google authorization-server metadata][google-oauth-metadata] publishes the
authorization-code and refresh-token grants, PKCE (`S256` and `plain`), the
authorization endpoint `https://accounts.google.com/o/oauth2/v2/auth`, token
endpoint `https://oauth2.googleapis.com/token`, and revocation endpoint
`https://oauth2.googleapis.com/revoke`. It publishes no dynamic client
registration endpoint. Jarvis therefore needs a pre-created Google OAuth client;
it must not fabricate RFC 7591 registration or borrow another product's client
credentials.

The strongest candidate for a native/headless Ubuntu runtime is Google's
documented installed-app authorization-code flow with PKCE, a loopback callback,
`access_type=offline`, and a human-opened consent URL. A browser on another
machine would require an explicit SSH loopback port forward (or another
separately reviewed Google flow); the removed out-of-band redirect is not an
option. This flow is a Google-API hypothesis, not a confirmed MCP contract:
no OAuth client was provisioned and no authenticated endpoint call was made.
Ticket 02 must not lock it until a supervised probe proves the client type,
redirect, granted scopes, identity, refresh behavior, and one call to each
service.

Under the current domain contract, keep the OAuth client secret and refresh
token only in `.env`; hold short-lived access tokens only in memory; and exclude
all OAuth material from model input, tool output, and runtime traces. Jarvis
never edits `.env`, so any required refresh-token rotation or writable token
store needs a separately reviewed domain amendment. Revocation/disconnection
must invalidate the current Google connection and any pending Google action
created under it. See Google's
[installed-app OAuth guide][google-installed-oauth] and
[OAuth production-readiness guidance][google-oauth-production].

The endpoint metadata advertises broad alternative scopes. The smallest
candidate grant for the currently selected operations is:

- Gmail reads: `https://www.googleapis.com/auth/gmail.readonly`; draft creation:
  `https://www.googleapis.com/auth/gmail.compose`.
- Drive reads/exports: `https://www.googleapis.com/auth/drive.readonly`.
- Calendar reads plus create/update: `https://www.googleapis.com/auth/calendar.events`.

These are candidate least-privilege scopes, not proof that one combined grant
works for all three hosted services. Ticket 02 and supervised authorization
must verify the exact granted scopes and identity. Google's
[OAuth scope policy][google-oauth-policy] makes restricted/sensitive scopes and
external production publication subject to consent-screen, verification, and
possibly security-assessment requirements; an app left in External/Testing can
also receive refresh tokens with a seven-day lifetime. That operational state
must be checked before claiming restart persistence.

### Current operations and selected input schemas

For reviewable drift evidence, the UTF-8 bytes of each complete, raw JSON
`tools/list` response from the request above were hashed on 2026-09-02:

| Service | Response bytes | SHA-256 |
| --- | ---: | --- |
| Gmail | 64,312 | `385e5cea2f07e68ca5d8c0c4582e68988fe4b44e3255c9d1fe61f4c9629716bc` |
| Drive | 24,089 | `4634a6d0f65eaff59af5f71c4cd729645b88d723f3135e045b56cf6fad6fee03` |
| Calendar | 84,758 | `cdf75cba52857c64334c38ca8f98ac8c65b49561a776bd22bdab680a4fcaf1f3` |

These whole-response digests cover every returned operation name, description,
annotation, input schema, and output schema. They are dated evidence, not a
vendor version or a promise that cosmetic description changes are compatible.

The live operation inventory is:

- Gmail: `create_draft`, `list_drafts`, `get_draft`, `get_thread`,
  `get_message`, `search_threads`, `label_thread`, `unlabel_thread`,
  `apply_sensitive_thread_label`, `trash_thread`, `untrash_thread`,
  `mark_thread_spam`, `unmark_thread_spam`, `list_labels`, `label_message`,
  `update_message_labels`, `unlabel_message`,
  `apply_sensitive_message_label`, `trash_message`, `untrash_message`,
  `mark_message_spam`, `unmark_message_spam`, and `create_label`.
- Drive: `copy_file`, `create_file`, `download_file_content`,
  `get_file_metadata`, `get_file_permissions`, `list_recent_files`,
  `read_file_content`, and `search_files`.
- Calendar: `list_events`, `get_event`, `list_calendars`, `suggest_time`,
  `create_event`, `update_event`, `delete_event`, `respond_to_event`, and
  `search_events`.

Only the following operations fit the current map. Property names shown below
retain the exact camel-case spelling returned by `tools/list`; the table is a
selected implementation summary, while the whole-response digests above pin the
complete observed schemas. All unlisted operations remain unavailable even if
discovery returns them:

| Operation | Required inputs | Optional inputs / bounds relevant to Jarvis |
| --- | --- | --- |
| `search_threads` | none | `query`, `pageSize` (default 20, max 50), `pageToken`, `includeTrash`, `view`; force `includeTrash=false` and a Jarvis cap |
| `get_thread` | `threadId` | `messageFormat`; select `PLAIN_TEXT` and bound returned content |
| `get_message` | `messageId` | `messageFormat`; select `PLAIN_TEXT` and bound returned content |
| `create_draft` | none | `to`, `cc`, `bcc`, `subject`, `body`, `htmlBody`, `replyToMessageId`, `attachments`; expose a narrower text-only schema if retained |
| `search_files` | none | `query`, `pageSize`, `pageToken`, `excludeContentSnippets`; require a bounded query/page size in the prepared tool |
| `get_file_metadata` | `fileId` | `excludeContentSnippets` |
| `read_file_content` | `fileId` | `includeComments`; default false and bound text output |
| `download_file_content` | `fileId` | `exportMimeType`; allow only a checked-in export MIME allowlist and bounded result |
| `search_events` | `query` | `pageSize`, `pageToken`; require a bounded page size |
| `list_events` | none | `calendarId`, `startTime`, `endTime`, `eventType`, deprecated `eventTypeFilter`, `fullText`, `orderBy`, `pageSize` (default 100, max 250), `pageToken`, `timeZone`; default to primary and impose a smaller cap/window |
| `get_event` | `eventId` | `calendarId`; default to primary |
| `create_event` | `summary`, `startTime`, `endTime` | calendar, time-zone, attendee, notification, recurrence, Meet, reminder, visibility, and other event fields; the approved action must freeze every supplied field |
| `update_event` | `eventId` | sparse patch fields for title/time/calendar/attendees/attachments/notifications/Meet/reminders/visibility; the approved action must freeze the exact patch and target |

`create_event` is non-idempotent. `update_event` is annotated idempotent by the
server, but Jarvis must still make every approved Google write exactly once and
must not retry an ambiguous transport result. Server annotations describe the
upstream tool; they do not grant Jarvis authority or override its approval
contract.

The checked-in operation manifest should store the complete discovered input
schema (or a canonical digest plus the reviewed narrowed Jarvis schema), endpoint,
protocol version, operation annotations, and capture date. Startup discovery
must fail closed if a selected name/schema/annotation changes; extra discovered
tools are ignored, never exposed.

### Preview, deployment, and pinning consequences

No authoritative Google-owned service page was found that defines Developer
Preview SLA, lifecycle, quota, support, changelog, deprecation, deployment, or
pinning terms for the three hosted Workspace endpoints. This is an evidence gap,
not proof that no such private/partner contract exists. It prevents this ticket
from treating "Developer Preview" as a known production contract. Live
discovery exposed `serverInfo.version = "ESF"` and the `/mcp/v1` route, but no
build date, digest, image, or selectable version. Jarvis can pin and verify the
dated observed response digests above; this research did not establish a way to
pin Google's hosted implementation.

The separately documented open-source Workspace server can be pinned to a
release tag or exact commit (the latest stable tag observed was `v0.0.8`, while
weekly `preview-*` tags also exist), and its README documents both local/headless
authentication and self-hosting infrastructure. It is nevertheless a local
server/Gemini CLI extension, not the remote Streamable HTTP service selected by
this effort. Adopting or wrapping it would reopen the explicitly excluded local
server lifecycle and deployment boundary.

The smallest honest next step is therefore a contract prototype—not production
activation—with three fixed remote endpoints, one Google connection, OAuth
credentials in `.env`,
a checked-in allowlist/schema snapshot, bounded prepared-tool adapters, and
fail-closed drift checks. Calendar create/update can proceed to an exact
one-attempt approval prototype. Gmail send/reply and the claim of a
production-capable official remote integration remain blocked until either
Google publishes and supports a suitable contract/tool or the map is explicitly
amended.

## Comments

- Resolved on 2026-09-02 from Google-owned catalog/source and OAuth documentation,
  plus reproducible unauthenticated protocol discovery against the three live
  Google-hosted endpoints. No Google account was authorized and no external data
  was read or changed.

[google-mcp-catalog]: https://github.com/google/mcp/blob/9ebafd607afdc06245323756a07221df23790a93/README.md
[workspace-extension]: https://github.com/gemini-cli-extensions/workspace/tree/089927ead01433f38c65c12cdcd2ed9a18165277
[mcp-streamable-http]: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http
[google-oauth-metadata]: https://accounts.google.com/.well-known/oauth-authorization-server
[google-installed-oauth]: https://developers.google.com/identity/protocols/oauth2/native-app
[google-oauth-production]: https://developers.google.com/identity/protocols/oauth2/production-readiness/policy-compliance
[google-oauth-policy]: https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification
