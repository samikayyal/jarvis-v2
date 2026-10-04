Type: task
Status: complete
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

- [x] Temporary-clone tests pass for every listed success, rejection, race, and
  uncertain-outcome path.
- [x] The full surviving suite and required static checks pass after the vault
  capability is composed into the runtime.
- [x] Deployment documentation and the system prompt direct the model to use
  `edit_vault` for vault changes and never use general terminal Git commands.
- [x] `--check` validates the optional vault configuration without contacting a
  provider or modifying the clone.
- [x] Candidate release validation is complete and the live replacement steps
  have a recorded rollback target before service activation.
- [x] A human-supervised real-remote acceptance proves one exact note edit,
  one commit, remote synchronization, and truthful reporting of the result.
- [x] No claim of live activation is made until the service, remote, and
  authorized phone interaction have all passed their respective gates.

## Comments

- This ticket owns verification and deployment preparation. Host installation,
  service replacement, and live acceptance require the deployment operator and
  are not implied by documentation changes in this ticket.


## Deployment evidence — 2026-10-04

- Implemented release: `e9fb255d6d821c2e1e12ec51366576045766b134`.
- Full surviving suite: 295 passed, 1 skipped. Ruff, formatting, compilation,
  and diff checks passed. The Windows CRLF fixture was corrected before the
  clean final suite run.
- Built the candidate on the host's installed Python 3.14.4 with hash-locked
  production dependencies; service-account `--check` passed before activation.
- The provider accepted the strict vault tool schemas. A live vault read
  synchronized successfully with GitHub.
- Activated the commit-named release. The service is active and enabled with
  zero restarts, its only listener is `172.20.0.1:9011`, OpenWA is healthy,
  and the configured named session is `ready`.
- The service-owned clone is `/var/lib/jarvis-personal-runtime/vault` and
  targets `ssh://git@github.com/samikayyal/obsidian-vault.git`, branch `main`.
  It is clean at `f7208d78bc7fabdce95ce9041238e4d5871d87c1`.
- The former clone `/var/lib/jarvis/vault` is preserved. Its unpublished
  commit `b89628fde69b52914274501767944d9fb30d88ef`, affecting
  `Projects/Jarvis/ideas.md`, was not replayed into the GitHub-backed clone.
- Post-deployment verification passed a disposable Ubuntu note update/create,
  exact approval, single commit, and push. A production-vault proposal was
  prepared and rejected, proving no live note or commit changed. The existing
  repository credential also passed a production push dry run.
- Previous runtime release `e4032ca18edf7c3cd31f61aed097ee7f39353ea7` and
  root-only configuration/prompt backups were retained for rollback.
- Initial acceptance limits: the initial deployment tests did not push an
  actual note to the personal remote or exercise a real authorized WhatsApp
  round trip. The live acceptance below closes those gaps. An Obsidian client
  pull remains unverified and is not claimed by this implementation completion.

## Live WhatsApp acceptance — 2026-10-04

- The operator explicitly authorized messaging Jarvis and testing the tools
  through the open in-app WhatsApp chat.
- `read_vault` searched successfully and returned a synchronized base. A new
  test-note proposal was rejected with `9`; the note remained absent and the
  clean vault HEAD did not change. Repeating the proposal and replying `1`
  created only `Projects/Jarvis/vault-tool-acceptance-2026-10-04.md` in commit
  `2fe522808e5a389d5f8ae57828ac1437a3050914`.
- An update proposal was cancelled with `/cancel`. A new exact proposal changing
  only `Status: created` to `Status: verified` was approved with `1`, committed,
  and synchronized in `eb2647e7802bad86a9ed18aa5fbafbc83f45269c`.
- Testing exposed misleading model narration about whether approval happened.
  Release `05bfec5c9b5077a7776475ad8fe40e0d814580cc` fixes this by returning
  explicit operator approval metadata to the model and explaining the runtime
  approval pause in the tool description. Its regression test reproduced the
  failure before the fix. Final suite: 295 passed, 1 skipped; changed-file Ruff
  and formatting checks passed. Candidate `--check` passed before deployment;
  release `e9fb255d6d821c2e1e12ec51366576045766b134` remains a rollback target.
- After deployment, Jarvis correctly acknowledged the approved update. A
  subsequent proposal changing `verified` to `rejected-test` was rejected with
  `9`; Jarvis correctly reported that the preview was shown, the proposal was
  rejected, and no changes were made. Its synchronized read showed `verified`.
- Independent service-account Git verification proved local HEAD and remote
  `main` both equal `eb2647e7802bad86a9ed18aa5fbafbc83f45269c`, a clean clone,
  exactly the two accepted test commits, and only the named test note changed.
  Final note SHA-256:
  `4aa3ba012e2823042c7b24367592d8cb11050df8a0d407878bbb834e6568466a`.
- The live release is `05bfec5c9b5077a7776475ad8fe40e0d814580cc`; service active
  and enabled, zero restarts, exact listener `172.20.0.1:9011`, OpenWA healthy,
  and configured named session `ready`. WhatsApp `/status` reports no active
  request and no pending action.
- The test note is retained as acceptance evidence. Receiving it in an
  Obsidian client still depends on that client's Git pull; no client pull was
  performed or verified in this test.
