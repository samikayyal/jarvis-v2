Type: task
Status: complete
Blocked by: 12

## Question

After explicit post-acceptance authorization, remove the obsolete control-plane source, tests, dependencies, workers, deployment assets, and active service components identified by the replacement inventory; retain OpenWA and historical `.scratch` records; rewrite active README, configuration examples, deployment guidance, and `CONTEXT.md` for the replacement; then run the complete surviving suite once.

Verify the new service remains active, OpenWA remains ready and paired, the old runtime cannot start, unrelated work is preserved, and Git contains the recoverable history of everything removed.

## Comments

- The authorized operator separately authorized post-acceptance retirement on
  2026-09-01 by explicitly invoking implementation of this Ticket 13 after
  Ticket 12 had recorded its final `GO` and reached `complete`.
- Live discovery before removal found the replacement active and enabled and
  OpenWA healthy with its sole `jarvis` named session `ready`. It also found
  retirement drift: the legacy Ubuntu backup timer remained enabled and the
  legacy Windows worker remained running with automatic startup. Both were
  retired as part of this ticket.

## Answer

The obsolete assistant control plane is retired from Git and the active hosts.
The complete `src/jarvis_control_plane/` package, its legacy tests and support
files, its 13-service Compose bundle, worker installers/units, backup and
acceptance assets, and the legacy-only `openai-agents` and `pyyaml` dependencies
were removed. The repository now contains only `src/jarvis_personal_runtime/`,
`tests/personal_runtime/`, and `deployment/personal-runtime/` in those active
source, test, and deployment boundaries. OpenWA documentation, the Baileys ADR,
agent workflow documentation, and all historical `.scratch` records remain.

The root README, native service deployment/operations guide, configuration
example, package metadata, checksum manifest, and `CONTEXT.md` now describe the
accepted personal assistant runtime, its prepared tools, exact approval model,
native Ubuntu and OpenSSH-over-Tailscale execution, private OpenWA handoff, and
the distinct messaging-gateway ownership boundary. The replacement package
contract additionally proves that no other source package or direct runtime
dependency survives.

Live retirement removed all 13 stopped `jarvis-assistant-v1` containers and
their private networks without removing volumes, disabled and removed the
legacy Ubuntu worker and backup units, and removed the validated `/opt/jarvis`
legacy code root. It also stopped and deleted the exact
`JarvisWindowsWorker` service and removed only its validated
`C:\ProgramData\Jarvis\worker` root. Inactive legacy state and credential
evidence outside those active components was preserved because it is not Git-
recoverable source and was not required to make the old runtime startable.

Post-retirement reconciliation proved:

- `/opt/jarvis-personal-runtime/current` still resolves to immutable accepted
  release `2be9b5419e4b707caedb3fc3e0f0bc8d3310dab8`; its service is enabled,
  active, and listening only on `172.20.0.1:9011`.
- OpenWA still uses the pinned image and `openwa-data` volume, reports healthy,
  and returns HTTP 200 with exactly one `jarvis` named session in `ready` state.
  No recent QR or `LOGOUT` event appeared.
- `/opt/jarvis`, all legacy Compose containers, the legacy Ubuntu worker and
  backup units, the Windows worker service, and its code root are absent. The
  old runtime therefore has no installed code, service, worker, or container
  start path.
- The working tree started clean, only the inventory-defined retirement and
  replacement documentation/package files changed, and the final repository
  boundary contains no unrelated source, test, or deployment deletion.
- Git can still resolve the removed legacy source at fixed point
  `332443f1989a7b5c5e795922d714c2dd4d18b64b`, so every repository removal is
  recoverable from committed history.

The required two-axis review compared `332443f...HEAD`. The Standards axis found
two canonical-language errors in the rewritten README/runbook: admission was
incorrectly assigned to OpenWA, and named/internal session identifiers plus
admitted text/ordinary request were blurred. Both were repaired. The Spec axis
found no scope creep but required this ticket to record the separate retirement
authorization and post-retirement live/suite evidence; this closure record
supplies both. No baseline code-smell finding remained.

Focused package, configuration, and service tests passed; Ruff, format checking,
and bytecode compilation passed. The complete surviving suite was run exactly
once at the end: 142 passed and one expected Windows-host POSIX process-group
test skipped in 17.83 seconds.
