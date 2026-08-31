Type: task
Status: complete
Blocked by: 04, 06, 07, 08, 09

## Question

Package the replacement as one native Ubuntu `systemd` service with owner-readable `.env`, editable TOML and `SYSTEM.md`, rotating trace storage, ordinary `journald` errors, a non-public listener reachable from OpenWA through the Docker host bridge, and straightforward installation, start, stop, status, and rollback instructions.

Validate the deployment without modifying the live OpenWA project, pairing state, firewall, service installation, or active old runtime.

## Answer

Packaged the replacement as one native Ubuntu service with a console entrypoint,
bounded HTTP/1.1 webhook listener, and explicit private-listener configuration.
The loader accepts only a configured RFC1918 IPv4 address and port, so the
rendered service can bind the exact Docker host-bridge gateway without a wildcard,
loopback, public, hostname, or IPv6 listener.

Added a replacement-specific deployment package containing a hardened `systemd`
unit template, `.env`, TOML, and `SYSTEM.md` examples, hash-locked runtime and
build dependencies, and checksums covering the complete replacement source and
package. Static `.env` credentials are root-owned and readable only by the
configured consuming service group; TOML and `SYSTEM.md` remain service-owned and
editable, and the existing rotating verbatim JSON Lines trace stays beneath the
private runtime root. Ordinary process output and errors go to `journald`.

The runbook now covers inactive validation, immutable commit-named release
staging, `uv` environment construction, discovery and recording of target-host
paths, service identity, bridge address and port, safe unit rendering, install,
start, stop, status, journal inspection, and rollback to the recorded previous
runtime. It explicitly prohibits Ticket 10 from touching the live OpenWA project,
pairing state, firewall, installed services, current release pointer, or active
old runtime.

## Comments

- Completed on 2026-08-31 with 139 replacement-runtime tests passing and one
  Ubuntu-only test skipped; Ruff, formatting, bytecode compilation, checksum,
  and diff checks were clean.
- The required specification and standards reviews found no remaining findings
  after replacement pinning, root-owned static credentials, shared composition,
  complete `uv` installation steps, and target-value deferral repairs.
- The one final repository-wide suite completed with 927 passed and 3 skipped.
  Its four failures were the expected immutable legacy deployment artifact-lock
  checks reporting `application source differs from the pinned artifact`; the
  retained rollback bundle was not weakened or repinned.
