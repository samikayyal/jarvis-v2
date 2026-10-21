# Direct terminal access verification

Implementation: `ff501df0b58c9e38eaa631f17b1e4b9be62a8bdf`.

- Focused skills, terminal, and packaging tests: 51 passed, 1 skipped.
- Full personal-runtime suite: 525 passed, 6 skipped in 340.96 seconds.
- Ruff lint, formatting (57 files), compileall, and diff checks passed.
- Hash-locked wheel installation and service-account configuration validation passed.
- Installed-package probe confirms direct `run_terminal` exposure and absence of
  the packaged terminal skill. Other skills retain their loading behavior.
- New tests cover first-request exposure, configured Ubuntu/Windows hosts, session
  reset, direct read-only execution, and approved/rejected commands through the
  real terminal approval handler with a fake executor.

No changes to credentials, configured model, saved permissions, or command policy.
The original main checkout's four modified vault/web files remain untouched.

Deployed release: `ff501df0b58c9e38eaa631f17b1e4b9be62a8bdf`.
Service active, NRestarts=0; OpenWA HTTP 200 and named session ready.
Live prompt equals the release example (SHA256
`33ca22c6f7c7b5b162bd29014139b10ee5b192fec3395136d6bac1672306ba0a`).
Configuration hash unchanged. Previous release `3cac27e` and prompt preserved in
`other/backups/terminal-core-ff501df0b58c9e38eaa631f17b1e4b9be62a8bdf/`.

At 2026-10-21T16:57:13Z, the live WhatsApp request exposed `run_terminal`
alongside `load_skill`; its catalog contained only obsidian, google, reminders,
and web. The system instructions included terminal guidance. The only tool call
was `run_terminal(host="ubuntu", command="pwd")`, with no skill load. Two API
exchanges returned HTTP 200. WhatsApp visibly reported
`/home/samik/Projects/Jarvis/code/legacy-workspace`.
