Type: task
Status: complete

# Delete Obsidian notes and verify the live deployment

## Contract

The operator requested whole-note deletion, then explicitly authorized deployment
and live testing through the open in-app WhatsApp chat. `delete_vault` accepts one
exact Markdown path, the synchronized base revision, and a commit message. It
shows the entire note and requires one-time approval for every deletion. Saved
edit permissions never authorize deletion, and choice `2` is unavailable.

Deletion shares the configured vault boundaries, frozen proposal, base and byte
revalidation, exact staging, single commit, and verified push workflow. Previous
content remains in Git history. `edit_vault` retains its create/update contract.

## Verification — 2026-10-05

- Implementation and deployed release:
  `e8bd4e2076d3c4ef16aff8a8f378fd8167b3914e`.
- Focused vault tests: 46 passed. Full suite: 325 passed, 1 skipped. Runtime and
  test Ruff checks, formatting, compilation, and diff checks passed.
- Built on host Python 3.14.4 using hash-locked production dependencies. The
  candidate passed service-account `--check` against the live configuration
  before activation. The live prompt received deletion guidance.
- Rollback release: `40184830fd90bfc3a22967a94949232014e6e5c7`. The previous
  prompt, configuration, and release path are retained in the root-only directory
  `/etc/jarvis/personal-runtime-backups/vault-delete-e8bd4e2`.
- WhatsApp created only
  `Projects/Jarvis/delete-tool-acceptance-2026-10-05.md` after approval `1`.
  Creation synchronized in `8b27f95f21d55393d054b6d4c273041f0573ca93`.
- The first `delete_vault` preview showed the whole note, the Git-history
  recovery explanation, and only `1`/`9` choices. Rejection with `9` left the
  note byte-for-byte unchanged and both local and remote HEAD unchanged.
  Note SHA-256 was
  `5421375d5522c69f4ceb832e0d0198fbd85e292cfa5bfde69a3cd27fd5f238dd`.
- A fresh deletion proposal was approved with `1`. Jarvis reported successful
  deletion and synchronization. The trace confirms `delete_vault`, `synced`,
  and `approved_once` with the preview shown.
- Independent service-account Git checks proved the note absent, a clean clone,
  and matching local HEAD and remote `main` at
  `59b1b3fc81e3a0ff52f48f3718c7167f1a8c1c16`. Exactly two test commits touched
  only the disposable note. The final complete vault tree equals pre-test
  revision `fd8bb5b7ef5f0aafd62f24a084c9527a7925a38e`; deleted content remains
  byte-for-byte recoverable from the creation commit.
- Final service: active, enabled, zero restarts, exact private listener
  `172.20.0.1:9011`. OpenWA remained healthy with its configured named session
  `ready`. Configuration and saved permissions are byte-for-byte unchanged.
- Final WhatsApp `/status`: no active request, no pending action, seven saved
  permissions. The disposable note was cleaned up through the new tool. No
  Obsidian client pull was performed or claimed.
