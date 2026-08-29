Type: task
Status: open
Blocked by: 04, 06, 07, 08, 09

## Question

Package the replacement as one native Ubuntu `systemd` service with owner-readable `.env`, editable TOML and `SYSTEM.md`, rotating trace storage, ordinary `journald` errors, a non-public listener reachable from OpenWA through the Docker host bridge, and straightforward installation, start, stop, status, and rollback instructions.

Validate the deployment without modifying the live OpenWA project, pairing state, firewall, service installation, or active old runtime.

## Comments
