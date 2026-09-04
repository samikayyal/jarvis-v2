Type: task
Status: complete
Blocked by: 04

## Question

With the operator supervising consent, real external writes, installation, and
go/no-go, connect the exact personal Google identity
`kayyal.sami@gmail.com` through bounded Google API tools backed by the
generally available official Gmail, Drive, and Calendar REST APIs; prove restart
persistence, bounded Gmail/Drive/Calendar reads, one delivered Gmail write, one
rejected write, one exact Calendar change, excluded mutations,
disconnect/reconnect behavior, and unchanged WhatsApp/vault/terminal paths;
then activate or roll back explicitly.

## Amendment (2026-09-03)

The active Google route is direct official Google APIs, not Google's hosted
Workspace Developer Preview MCP endpoints. Personal Gmail identities cannot
enroll in that preview program, and the hosted Gmail MCP has no send/reply
operation. The generic `ConfiguredMcpService` capability remains implemented
for separately configured services but is not the active route for this Google
connection. The existing bounds, exact approval grammar, one-attempt writes,
trace redaction, Drive read-only boundary, supervised OAuth and external writes,
and human go/no-go remain unchanged. This issue stays `claimed` until real
activation acceptance passes.

## Answer

Activated the bounded direct Google API route for the exact personal identity
`kayyal.sami@gmail.com` after the operator's explicit go decision on
2026-09-04. The active route uses Google's generally available Gmail, Drive,
and Calendar REST APIs; the retained generic configured-MCP capability is not
configured for this Google connection.

Sanitized implementation and review evidence:

- The direct adapter exposes only the fixed Gmail, Drive, and Calendar tool
  set, keeps Drive read-only, and requires one exact approval for Gmail and
  Calendar writes. Connection replacement, disconnect, and restart invalidate
  pending writes, and writes are never retried automatically.
- A live empty Gmail collection exposed an HTTP `204` mapping defect. Commit
  `a77126a` repaired collection reads while preserving fail-closed behavior for
  single-resource reads; commit `fd7f0e9` added equivalent Drive and Calendar
  regression coverage without changing production behavior.
- Focused Google adapter tests passed with `20 passed`. Ruff check, Ruff format
  check, Python compilation, and `git diff --check` passed. The one final
  repository-wide suite on the final production implementation passed with
  `197 passed, 1 skipped`; the later delta was tests only. Both standards and
  specification reviews reported no remaining finding.

Sanitized supervised production evidence:

- OAuth verified the exact identity and required identity, Gmail read/send,
  Drive read-only, and Calendar-events scopes without exposing credential
  material. The active configuration contains one `[google]` route and no
  configured MCP service.
- Bounded Gmail, Drive, and Calendar reads succeeded through their direct REST
  tools. The repaired exact Gmail absence query returned `count=0`.
- One rejected Gmail fixture produced no Google write. One approved Gmail
  fixture produced exactly one successful POST and remained exactly
  `count=1`; no duplicate was created.
- One approved primary-calendar fixture produced exactly one successful POST
  and remained exactly `count=1`. The accepted event remains in the calendar.
- Gmail deletion and archive/label changes, Drive mutation, Calendar deletion,
  and arbitrary Google operations were all refused without tool calls,
  approval choices, or Google exchanges.
- Disconnect/reconnect invalidated an exact pending Gmail action; the stale
  approval caused zero Gmail POSTs, and a bounded read succeeded after
  reconnect.
- A second pending Gmail action was present immediately before the supervised
  service restart. After restart, `/status` reported no active request or
  pending action, Google reported disconnected, and the stale approval caused
  zero approval choices, tool calls, or Google exchanges. After reconnect, its
  exact-subject Gmail search returned `count=0`.
- The unchanged WhatsApp, vault, and Ubuntu terminal paths passed. The vault
  and terminal probes each made exactly one expected tool call with no approval,
  Google exchange, or tool error.
- Release `fd7f0e9` is active and release `7ce88ed` is the retained rollback
  target. Jarvis is active and enabled with zero restarts and a private-only
  listener. OpenWA was not restarted or recreated; its container remains
  healthy with zero restarts and its exact configured session remains uniquely
  ready with the engine loaded. Runtime credential and configuration modes are
  correct, and temporary staging material was removed.

The operator gave the final explicit go decision after this reconciliation, so
the direct Google API integration remains active.

## Comments

- Claimed and preflighted on 2026-09-03 without authorizing Google, installing
  credentials, changing the target host, restarting services, performing an
  external write, or activating production.
- Live unauthenticated discovery still returns 23 Gmail operations, 8 Drive
  operations, and 9 Calendar operations. Every selected operation digest,
  server identity, and protocol version still matches the checked-in manifests.
  Gmail's 23 operations still include draft creation but no send or reply.
- Consequently the required delivered Gmail write remains impossible through
  the locked official remote-MCP boundary. Activation must stop until the map is
  explicitly amended or Google exposes and the implementation safely adds a
  suitable official operation.
- The target Tailscale device is online, but neither ordinary SSH nor Tailscale
  SSH passed the existing trust gates: ordinary SSH had no accepted credential,
  and Tailscale SSH presented an ED25519 host key that is not in this machine's
  known-hosts file. No host key was accepted automatically.
- This process has none of the required Google OAuth, OpenAI, or OpenWA
  environment variables. Exact Google identity, consent, credential placement,
  host-key trust, installation, external-write fixtures, and final go/no-go
  therefore remain operator-owned gates.
- Supervised activation was attempted on 2026-09-03 with the operator's exact
  authorization. The new release, private configuration, exact Google identity,
  restart persistence, connection controls, and bounded no-content-output MCP
  probes passed. A production Responses request initially exposed an invalid
  strict-schema advertisement for Calendar writes; commits `92f2cde` and
  `e5fa351` repaired and hardened it with regression coverage. The final suite
  passed with 175 tests and one existing platform-conditional skip; both review
  axes then reported no remaining finding.
- Real WhatsApp acceptance reached all three official MCP tools. Google denied
  Gmail and Calendar because Cloud project `915023365865` is not enrolled in
  the Google Workspace Developer Preview Program, and denied Drive with `The
  caller does not have permission`. No real read or write succeeded.
- Google's official enrollment form explicitly rejects personal Gmail
  identities and requires an email in a Google Workspace domain. The authorized
  identity is a personal Gmail address, so this project cannot be enrolled with
  that identity. Google documents that an eligible application normally takes
  a couple of days to approve.
- Because real acceptance could not pass, production was explicitly rolled back
  to the verified pre-Google configuration and rebuilt Python 3.14-compatible
  prior release. Jarvis returned active with zero restarts, OpenWA remained
  healthy with zero restarts, the private listener returned, and the active
  `.env`/`jarvis.toml` contain no Google credentials or MCP services. Candidate
  OAuth material remains inactive in root-controlled files for a future eligible
  Workspace identity; it was neither printed nor copied into the repository.
- The architecture was amended on 2026-09-03 for the personal account
  `kayyal.sami@gmail.com`: implement the bounded direct Google API route with
  OAuth scopes for identity, Gmail read/send, Drive read-only, and Calendar
  events, then repeat the supervised acceptance before any activation.
