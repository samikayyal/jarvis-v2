Type: task
Status: open
Blocked by: 11

## Question

With the authorized operator live, install and activate the prepared native service, supply `.env`, connect only the private OpenWA handoff, and prove real WhatsApp-to-OpenAI reply flow, automatic read-only Ubuntu and Windows commands, explicitly approved mutating commands on both hosts, cancellation, trace capture, preserved OpenWA readiness and pairing, and confirmed phone receipt.

Stop for operator approval at credentials, installation/replacement, every mutating smoke-test proposal, phone receipt, and the final go/no-go. Preserve the previous runtime for rollback until every row passes.

## Comments

- Live acceptance was performed with the authorized operator on 2026-08-31 and
  2026-09-01. Credential reuse, replacement installation, the one controlled
  OpenWA allowlist recreation, the exact source-specific UFW rule, and each
  mutating smoke-test proposal received operator approval before execution.
- The final operator go/no-go remains pending. Keep this ticket open and retain
  the complete stopped legacy runtime until that decision is recorded.

## Answer

The native personal assistant runtime is installed from immutable release
`2be9b5419e4b707caedb3fc3e0f0bc8d3310dab8`, enabled, active, and listening only
on Docker bridge gateway `172.20.0.1:9011`. Its protected configuration remains
root/service-owned with modes `0440`, `0600`, and `0600`. The previous 13-service
Compose runtime and its immutable release remain installed and stopped; its
Ubuntu worker is disabled and inactive.

The preserved OpenWA container still uses the pinned image and `openwa-data`
volume. It is `healthy`; exactly one authenticated named session is `ready`; no
`LOGOUT` or fresh QR event occurred. The single active `message.received` webhook
targets `http://172.20.0.1:9011/webhook` with retry count three. OpenWA's allowlist
contains only the rollback hostname plus the bridge gateway. One UFW rule admits
TCP 9011 only from the current OpenWA handoff address on the exact bridge
interface; an unsigned in-container probe reaches the listener and receives
HTTP 401. The listener has no wildcard bind.

Live WhatsApp acceptance passed:

- A real authorized message traversed OpenWA, the replacement runtime, OpenAI
  Responses, and OpenWA outbound exactly once; the operator confirmed the exact
  `READY12B` phone reply. Deterministic `/new` and `/status` replies also passed.
- Automatic Ubuntu `pwd` returned `/srv/jarvis-workspace`; automatic Windows
  `Get-Location` returned `D:\Projects\Jarvis-v2`. Both exited zero without an
  approval event. The Windows route used the dedicated key, strict pinned host
  key, and ordinary OpenSSH over Tailscale.
- Separately approved one-time Ubuntu and Windows mutation commands each created
  and removed one unique empty smoke-test file, exited zero, returned the exact
  `UBUNTU_MUTATION_OK` and `WINDOWS_MUTATION_OK` markers, and left no file.
- Exact `9` rejection and pending-action `/cancel` each produced zero execution
  events and no file. An approved `sleep 120` command entered active execution,
  `/cancel` arrived about 2.6 seconds later, the local process group was
  cancelled, no process or late reply remained, and the conservative
  cancellation notice reached WhatsApp.
- Verbatim JSON Lines traces contain admission, request, complete Responses HTTP
  attempts and payloads, tool calls/results, proposals, approval decisions,
  Ubuntu and Windows execution, cancellation, and outbound reply events.

Live acceptance exposed three Responses transcript defects and one invalid
initial smoke-test location. Commits `e6f9824`, `1aae723`, and `4137ee7` repair
response-only output-field replay and pending-action cancellation continuity at
the public `DirectResponsesRunner`/runtime seams. The failed initial Ubuntu
mutation never created its file. Focused replacement verification after the
repairs passes with 141 tests and one expected Windows-host POSIX skip; Ruff,
formatting, compilation, package checks, and checksum validation pass.

Production reconciliation is settled: replacement restart count zero, private
listener and firewall rule exact, OpenWA healthy/ready with pairing retained,
Windows key-only execution succeeds, legacy containers stopped and preserved,
old worker inactive, disk use 9%, and host load low. Final lifecycle status and
the map decision must be changed to `complete` only after the final operator
go/no-go.
