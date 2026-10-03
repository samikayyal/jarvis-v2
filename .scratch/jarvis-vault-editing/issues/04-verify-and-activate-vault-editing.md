Type: task
Status: claimed
Blocked by: 01, 02, 03

# Verify and activate vault editing

## Question

What evidence is required before enabling the Git-backed vault capability in a
personal-runtime deployment, and how should the operator recover from a
partial or uncertain synchronization result?

## Scope

- Add focused tests using temporary Git clones and a local bare remote. Cover
  configuration parsing, read-only compatibility, synchronized reads, stale
  reads, path/content validation, exact replacements, proposal freezing,
  approval/cancellation/restart, clean execution, base races, dirty state,
  staged-diff mismatch, rejected pushes, push timeouts, remote verification,
  and manual recovery.
- Run the normal repository checks with `uv`: focused vault tests, the surviving
  personal-runtime suite, Ruff, formatting, compilation, and `git diff --check`.
- Document the optional `[vault_git]` deployment configuration, service-account
  ownership, SSH key and pinned known-hosts requirements, fresh-read behavior,
  one-time approval, result states, and recovery procedure.
- Validate a candidate release and configuration with `--check` before any
  service replacement. Keep the vault clone, SSH key, known-hosts file, and
  runtime data outside the application release directory.
- For supervised activation, use a disposable or approved test remote first,
  then perform one small real Markdown create/update, verify the commit on the
  configured remote, and confirm that Obsidian can pull it. Preserve the
  existing OpenWA and runtime health gates.

## Acceptance criteria

- [ ] Temporary-clone tests pass for every listed success, rejection, race, and
  uncertain-outcome path.
- [ ] The full surviving suite and required static checks pass after the vault
  capability is composed into the runtime.
- [ ] Deployment documentation and the system prompt direct the model to use
  `edit_vault` for vault changes and never use general terminal Git commands.
- [ ] `--check` validates the optional vault configuration without contacting a
  provider or modifying the clone.
- [ ] Candidate release validation is complete and the live replacement steps
  have a recorded rollback target before service activation.
- [ ] A human-supervised real-remote acceptance proves one exact note edit,
  one commit, remote synchronization, and truthful reporting of the result.
- [ ] No claim of live activation is made until the service, remote, and
  authorized phone interaction have all passed their respective gates.

## Comments

- This ticket owns verification and deployment preparation. Host installation,
  service replacement, and live acceptance require the deployment operator and
  are not implied by documentation changes in this ticket.

