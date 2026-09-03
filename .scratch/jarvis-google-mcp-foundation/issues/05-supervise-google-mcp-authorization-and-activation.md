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
