# Google access and OAuth boundary

Research date: 2026-08-01

This note resolves the research question for the locked Jarvis Assistant V1
matrix in [`map.md`](../map.md): Gmail, Calendar, and Drive reads are allowed;
Gmail sends and Calendar changes are approval-gated; destructive Google and
all Drive actions are excluded. It does not provision OAuth credentials,
access a Google account, or implement an API connector.

## Decision

Use one Google OAuth 2.0 **Web application** client for the server-side Jarvis
backend, with the authorization-code flow and one durable credential record for
the one authorized Google account. Google documents the web-server scenario as
the flow in which the server redirects a browser to Google, receives an
authorization code, exchanges it for access and refresh tokens, and stores the
refresh token for later use. A service account is not the right V1 identity:
it represents the application rather than the operator's personal Gmail,
Calendar, and Drive account. [Google OAuth 2.0 overview](https://developers.google.com/identity/protocols/oauth2#webserver)

Use an **External** OAuth audience for the personal account unless the Google
Cloud project and account are definitively inside the same Google Workspace
organization. For durable V1 operation, move the External app to **In
production** after the required consent/verification work. Keep **Testing** for
development only: Google states that an External app in Testing receives a
refresh token that expires after seven days when it requests service scopes.
This avoids the documented seven-day test limitation, but does not make a
refresh token permanently valid; normal revocation and expiration handling is
still required. [OAuth consent configuration](https://developers.google.com/workspace/guides/configure-oauth-consent),
[OAuth app audience and testing behavior](https://support.google.com/cloud/answer/15549945),
[OAuth refresh-token behavior](https://developers.google.com/identity/protocols/oauth2#expiration)

The initial authorization should request the read capabilities required by the
first V1 tool set. Request the write scopes incrementally when the operator
first uses the corresponding feature, and explain the feature before showing
the new consent screen. Google recommends incremental authorization and
requesting the smallest scope set in context. [OAuth web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server),
[OAuth security best practices](https://developers.google.com/identity/protocols/oauth2/resources/best-practices)

## Exact V1 OAuth flow

1. Jarvis starts an authorization request from the backend and opens the
   Google authorization URL in the operator's normal, full-featured browser.
   Do not use a mobile or desktop embedded webview. [OAuth security best practices](https://developers.google.com/identity/protocols/oauth2/resources/best-practices#use_secure_browsers)
2. The request uses `response_type=code`, an HTTPS redirect URI registered for
   the Web application client, the exact requested scopes, `access_type=offline`,
   `include_granted_scopes=true`, and a cryptographically random `state` value.
   Google requires the redirect URI to match the registered URI exactly,
   including scheme, case, and trailing slash. The callback must reject a
   mismatched `state` before exchanging the code. [Web-server authorization parameters](https://developers.google.com/identity/protocols/oauth2/web-server#creatingclient)
3. Google authenticates the account and displays the consent screen. The
   operator may grant or deny individual scopes where granular consent is
   available. A denied scope disables only the dependent Jarvis capability;
   it is not treated as approval for any action.
4. The backend exchanges the code at
   `https://oauth2.googleapis.com/token`, stores the returned refresh token in
   the protected token store, and redirects the browser to a clean result page
   so the code is not left in the URL or referrer. [Web-server callback and token exchange](https://developers.google.com/identity/protocols/oauth2/web-server#handlingresponse)
5. Jarvis uses short-lived access tokens in the Google API connector and
   refreshes them server-side when needed. `openid` may be included solely to
   bind the one credential record to Google's stable `sub` identifier; the
   backend must reject a later authorization for a different `sub` rather than
   silently replacing the account. Google documents `sub` as a stable unique
   Google Account identifier in its OpenID Connect reference. [OpenID Connect reference](https://developers.google.com/identity/openid-connect/reference#userinfo)

`prompt=consent` is appropriate for the initial connection or an explicit
reconnect when a refresh token is needed again. It should not be used as a
substitute for the normal refresh flow: Google documents that the refresh token
is normally returned on the first authorization, and the client library
examples warn that reauthorization may be needed to obtain a missing refresh
token. [Web-server authorization parameters](https://developers.google.com/identity/protocols/oauth2/web-server#creatingclient)

The exact public HTTPS callback hostname is not decided by the current map.
That is a deployment detail that must be selected and registered before
implementation; OAuth out-of-band callbacks are not a V1 fallback.

## Least-privilege scopes and APIs

The following is the exact V1 baseline. A scope grants Google API capability;
it does not grant Jarvis permission to perform a particular action. The
application-level action matrix below remains authoritative.

| Service | V1 API methods | Initial read scope | Incremental write scope | Boundary |
| --- | --- | --- | --- | --- |
| Gmail | `users.messages.list`, `users.messages.get`, `users.threads.list`, `users.threads.get` | `https://www.googleapis.com/auth/gmail.readonly` | `https://www.googleapis.com/auth/gmail.send` | Read mail without modifying it; send only after Jarvis approval. Do not download attachments in text-only V1. |
| Calendar | `calendarList.list`, `events.list`, `events.get` | `https://www.googleapis.com/auth/calendar.calendarlist.readonly` and `https://www.googleapis.com/auth/calendar.events.readonly` | `https://www.googleapis.com/auth/calendar.events` | Read calendars/events; create or change events only after Jarvis approval. |
| Drive | `files.list`, `files.get` with metadata or `alt=media`, and `files.export` for Google Workspace documents | `https://www.googleapis.com/auth/drive.readonly` | None | Read and export existing Drive content; expose no Drive mutation. |

The scope choices follow these API facts:

- Gmail `messages.list` returns message identifiers and `messages.get` fetches
  the message; both accept `gmail.readonly`. Gmail's scope guide classifies
  `gmail.readonly` as the read scope and `gmail.send` as the narrower send
  scope. Do not request `https://mail.google.com/`, `gmail.modify`, or
  `gmail.compose` for the locked V1 surface. [Gmail messages.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list),
  [Gmail messages.get](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get),
  [Gmail messages.send](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send),
  [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- Calendar's `calendarList.list` accepts
  `calendar.calendarlist.readonly`; event listing and retrieval accept
  `calendar.events.readonly`. Event creation, full update, and partial update
  use `events.insert`, `events.update`, and `events.patch`, respectively, and
  accept `calendar.events`. The broader `calendar` scope is not needed. The
  `calendar.events.owned` / `.owned.readonly` alternatives are narrower if a
  later V1 decision limits Jarvis to calendars owned by the operator; the
  current map does not impose that restriction, so this artifact covers the
  operator's accessible event calendars. [Calendar scopes](https://developers.google.com/workspace/calendar/api/auth),
  [CalendarList.list](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/list),
  [Events.list](https://developers.google.com/workspace/calendar/api/v3/reference/events/list),
  [Events.get](https://developers.google.com/workspace/calendar/api/v3/reference/events/get),
  [Events.insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert),
  [Events.update](https://developers.google.com/workspace/calendar/api/v3/reference/events/update),
  [Events.patch](https://developers.google.com/workspace/calendar/api/v3/reference/events/patch)
- `calendar.freebusy` is not in the baseline because the locked map does not
  require a separate free/busy tool. If V1 adds that read surface, request
  `https://www.googleapis.com/auth/calendar.freebusy` incrementally and expose
  only `freebusy.query`; do not broaden to `calendar.readonly`. [Freebusy.query](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query)
- Drive's `files.get` can return binary content with `alt=media`, while
  `files.export` returns exported bytes for Google Docs, Sheets, and Slides.
  Therefore `drive.metadata.readonly` is insufficient for the locked
  content-read capability. Google documents `drive.readonly` as viewing and
  downloading all Drive files, and classifies it as restricted. `drive.file`
  is safer and non-sensitive but only covers files opened or shared with the
  app through a picker/file picker; it does not satisfy broad existing-Drive
  reads. [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth),
  [Drive files.list](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list),
  [Drive files.get](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/get),
  [Drive files.export](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export)

Gmail and Drive scopes in this baseline are broad user-data scopes. Google
classifies `gmail.readonly` as restricted, and classifies `drive.readonly` as
restricted; Google says restricted-scope apps may require OAuth verification
and that storing or transmitting restricted-scope data on servers requires a
security assessment. The personal-use/testing exception can affect whether
verification is mandatory for a one-user app, but it does not remove the need
to review the current verification and security-assessment requirements before
production. [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes),
[Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth),
[OAuth verification overview](https://support.google.com/cloud/answer/13463073)

## Tool-level action matrix

OAuth consent is not an action approval. Once the operator grants a write
scope, Jarvis still must apply this deterministic matrix on every tool call.

| Tool surface | API operation | V1 result | Required handling |
| --- | --- | --- | --- |
| Gmail read | `users.messages.list/get`, `users.threads.list/get` | Allowed without action approval | Fixed read-only connector; no label, draft, settings, or attachment mutation/download surface. |
| Gmail send | `users.messages.send` | Approval-gated | Freeze recipients, subject, body, and any other supported fields in the pending action; send only after exact confirmation. The `gmail.send` scope alone never authorizes an unapproved send. |
| Gmail other writes | `drafts.*`, `messages.modify`, `messages.trash`, `messages.delete`, `batchDelete`, settings, filters, forwarding, delegation | Excluded | No V1 tool and no extra scope. |
| Calendar read | `calendarList.list`, `events.list/get` | Allowed without action approval | Read-only connector; `calendarId` and event IDs come from the authorized account's accessible calendars. |
| Calendar change | `events.insert`, `events.update`, `events.patch` | Approval-gated | Freeze calendar, event identity, complete resulting event, attendees, recurrence, visibility, reminders, and notification choice in the pending action. For a partial change, fetch then update with an ETag or use a carefully reviewed patch; Calendar documents that patch array fields replace existing arrays. [Events.update](https://developers.google.com/workspace/calendar/api/v3/reference/events/update), [Events.patch](https://developers.google.com/workspace/calendar/api/v3/reference/events/patch) |
| Calendar destructive/other writes | `events.delete`, `events.move`, `events.import`, `events.clear`, calendar-list writes, calendar property writes, ACL/sharing writes | Excluded | No V1 tool and no scope expansion. Calendar delete remains excluded even though the selected event-write scope could technically authorize it. |
| Drive read | `files.list`, `files.get`, `files.export` | Allowed without action approval | Read/export only; apply the text-only assistant boundary to content returned to Jarvis. |
| Drive mutation | Create, upload, copy, move, rename, update, delete, permissions, comments, revisions, labels, or sharing changes | Excluded | No Drive write scope and no V1 tool. |

Calendar writes can have external effects beyond the event record. Google
documents that event create/update requests have notification controls and that
some emails may still be sent in certain cases. The exact notification mode
and attendee set therefore belong in the proposal the operator approves; the
connector must not silently infer approval from the OAuth grant. [Events.insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert),
[Events.update](https://developers.google.com/workspace/calendar/api/v3/reference/events/update)

## Token and credential boundary

- The OAuth client ID and client secret are backend configuration, not source
  code, Git, the Obsidian vault, WhatsApp messages, or model context. Google
  says client credentials must be protected and not committed to repositories.
  [OAuth security best practices](https://developers.google.com/identity/protocols/oauth2/resources/best-practices#handle_client_credentials_securely)
- The authorization code exists only during the callback exchange. The refresh
  token is the only long-lived user credential and is stored once, encrypted at
  rest, in a private backend token store keyed to the accepted Google `sub`.
  Google requires secure long-term refresh-token storage and recommends
  encryption and a datastore that is not publicly reachable for server-side
  applications. [OAuth overview](https://developers.google.com/identity/protocols/oauth2),
  [OAuth security best practices](https://developers.google.com/identity/protocols/oauth2/resources/best-practices#handle_user_tokens_securely)
- Access tokens are short-lived runtime credentials. Keep them in the Google
  connector's process memory or an equivalent protected runtime cache, never in
  persistent notes, logs, prompts, tool arguments, or outbound WhatsApp text.
  Send them only in the HTTPS `Authorization: Bearer` header to Google APIs;
  Google specifically warns against putting access tokens in URI parameters
  because they can reach logs. [OAuth overview](https://developers.google.com/identity/protocols/oauth2)
- The model and general tool dispatcher receive typed, allowlisted operations
  and sanitized API results. They never receive or select raw OAuth tokens,
  client secrets, arbitrary Google endpoints, or arbitrary scopes. The
  Google connector is the only component allowed to refresh tokens or call the
  Google APIs.
- A new consent grant for a write scope must not execute a pending Gmail or
  Calendar action. Scope acquisition and the exact action confirmation are two
  separate state transitions.

## Revocation, expiry, and recovery

1. On operator disconnect, explicit revoke, or removal of the integration,
   POST the refresh token (or access token) to
   `https://oauth2.googleapis.com/revoke`, then delete all local access and
   refresh-token material. Google documents an empty `200 OK` for successful
   revocation and `invalid_token` when the token is already expired or revoked.
   [OAuth revocation endpoint](https://developers.google.com/identity/openid-connect/reference#revocation)
2. If a refresh fails with `invalid_grant` or equivalent invalid-credential
   behavior, stop Google calls, mark the single connection as disconnected,
   discard the local credential, and ask the operator to reconnect. Do not
   retry indefinitely and do not execute a queued write after reconnect. Google
   documents user revocation, six months of refresh-token inactivity, Gmail
   password changes, refresh-token limits, time-based access, and policy
   changes as possible invalidation causes. [OAuth refresh-token expiration](https://developers.google.com/identity/protocols/oauth2#expiration),
   [OAuth web-server error handling](https://developers.google.com/identity/protocols/oauth2/web-server#handlingresponse)
3. Testing-mode expiry is a planned development failure: an External app in
   Testing with service scopes can lose its refresh token after seven days.
   Production V1 must not rely on a Testing token. [OAuth app audience and testing behavior](https://support.google.com/cloud/answer/15549945)
4. Keep only one live credential record for the one V1 Google account. Google
   documents a per-client limit of 100 live refresh tokens per account and says
   that issuing another token beyond the limit invalidates the oldest without
   warning. Avoid repeated reauthorization and token churn. [OAuth refresh-token limits](https://developers.google.com/identity/protocols/oauth2#expiration)

## Confirmed facts, recommendations, and bounded uncertainty

### Confirmed by Google documentation

- Web-server OAuth uses a browser consent step, authorization code exchange,
  access token, and refresh token; `offline` access is required for refresh
  while the user is absent.
- The listed Gmail, Calendar, and Drive methods accept the listed scopes.
- Access tokens are scope-limited and short-lived; refresh tokens can be
  revoked or expire; revocation has a documented endpoint.
- Drive content reads require more than metadata-only access, while `drive.file`
  is per-file and narrower than broad existing-Drive access.

### V1 recommendations made here

- Use a Web application client, External audience, production publishing for
  durable operation, one credential record, incremental write scopes, and a
  backend-only token boundary.
- Treat `gmail.send`, `calendar.events`, and all Calendar event mutations as
  capability grants that never bypass deterministic Jarvis approval.
- Use `drive.readonly` because the locked phrase “Drive reads” is interpreted
  as existing-file metadata and content reads, not metadata-only search. If the
  product later narrows that phrase to curated files, switch to `drive.file`
  with a picker instead of expanding write access.

### Bounded uncertainty to carry into implementation

- The deployment's exact HTTPS OAuth callback URL is not present in the map and
  must be selected before creating the client.
- Google verification and security-assessment obligations depend on the final
  audience, publishing status, requested scopes, and server-side data handling.
  The one-user personal-use exception is not a substitute for checking the
  current Google Cloud Console and policy path.
- The map does not say whether Calendar writes must be limited to calendars the
  operator owns. This note preserves the locked broad Calendar-read wording and
  uses `calendar.events` for accessible event calendars; a later owned-only
  decision can narrow to `calendar.events.owned` and
  `calendar.events.owned.readonly` without changing the approval matrix.

## Primary sources

All sources used for this note are first-party Google documentation:

- [Using OAuth 2.0 to Access Google APIs](https://developers.google.com/identity/protocols/oauth2)
- [Using OAuth 2.0 for Web Server Applications](https://developers.google.com/identity/protocols/oauth2/web-server)
- [OAuth 2.0 Best Practices](https://developers.google.com/identity/protocols/oauth2/resources/best-practices)
- [Google OpenID Connect API Reference](https://developers.google.com/identity/openid-connect/reference)
- [Configure the OAuth consent screen and choose scopes](https://developers.google.com/workspace/guides/configure-oauth-consent)
- [Choose Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Gmail REST method reference](https://developers.google.com/workspace/gmail/api/reference/rest)
- [Choose Google Calendar API scopes](https://developers.google.com/workspace/calendar/api/auth)
- [Google Calendar REST method reference](https://developers.google.com/workspace/calendar/api/v3/reference)
- [Choose Google Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Google Drive REST method reference](https://developers.google.com/workspace/drive/api/reference/rest/v3)
- [Google Cloud: Manage app audience](https://support.google.com/cloud/answer/15549945)
- [Google Cloud: OAuth App Verification](https://support.google.com/cloud/answer/13463073)
