Type: task
Status: complete
Blocked by:

# Configure the Git-backed vault and synchronized reads

## Question

How should the existing read-only `read_vault` capability use a dedicated Git
clone while preserving current deployments that only configure `vault_path`?

## Scope

- Keep `vault_path` as the canonical vault root. Without `[vault_git]`, retain
  the current bounded local read-only behavior and do not expose `edit_vault`.
- Add an optional `[vault_git]` configuration section. When present, require
  `vault_path` and validate `remote`, `branch`, `note_directories`,
  `author_name`, `author_email`, `ssh_identity_file`, and
  `ssh_known_hosts_file` according to the runtime configuration boundary.
- Treat `vault_path` as one dedicated service-owned clone, separate from the
  Jarvis application checkout. Validate its Git root, configured remote,
  branch, repository ownership assumptions, and allowed note directories.
- Run bounded Git argument vectors with a controlled environment and pinned SSH
  identity/known-hosts file. Do not invoke `run_terminal`, a shell, or a model
  supplied Git argument.
- Before a fresh Git-backed read, fetch the configured remote and fast-forward a
  clean clone. Reject dirty state, unexpected branch state, non-fast-forward
  updates, and repository layouts that cannot be proved safe.
- Return a full base revision and an explicit freshness state with read results.
  A clean local clone may serve a stale read after synchronization is
  unavailable only when the result discloses that it is stale. A write must
  have a successfully synchronized base.

## Acceptance criteria

- [x] A configuration without `[vault_git]` passes existing `read_vault`
  configuration and behavior tests unchanged.
- [x] A `[vault_git]` configuration without `vault_path`, with invalid paths,
  invalid note directories, or incomplete SSH settings is rejected before the
  service starts.
- [x] Valid configuration exposes the Git-backed vault settings to the vault
  tool without exposing private key contents or credentials to model context.
- [x] A clean clone fetches and fast-forwards before a fresh read and returns
  the resulting full commit revision.
- [x] Remote unavailability produces an explicitly stale read only when the
  clone is clean; no write preparation can proceed from that state.
- [x] Dirty, detached, wrong-branch, non-fast-forward, and unexpected-remote
  states stop safely without a merge, rebase, or automatic repair.
- [x] Git subprocesses use no shell and are covered by tests that assert the
  configured remote, branch, SSH identity, and known-hosts boundary.

## Comments

- This ticket establishes the read and configuration seam used by the remaining
  tickets. It does not itself apply note edits or deploy to the live host.

Implementation and focused verification completed on 2026-10-04. Real temporary Git repositories cover the read/edit/approval/commit/push contract; configuration, Responses continuation, rejection, stale reads, base races, CRLF, empty-note creation, and uncertain push outcomes pass.

