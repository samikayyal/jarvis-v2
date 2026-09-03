Type: task
Status: claimed
Blocked by: 04

## Question

With the operator supervising consent, real external writes, installation, and
go/no-go, connect the exact Google identity; prove restart persistence, bounded
Gmail/Drive/Calendar reads, one delivered Gmail write, one rejected write, one
exact Calendar change, excluded mutations, disconnect/reconnect behavior, and
unchanged WhatsApp/vault/terminal paths; then activate or roll back explicitly.

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
