# Jarvis vault editing and Git synchronization

## Status

Implemented, deployed, and tested through the authorized WhatsApp chat on
2026-10-04. Current application release:
`05bfec5c9b5077a7776475ad8fe40e0d814580cc`. Automated checks, live approval,
rejection, cancellation, exact note create/update, and independent GitHub
synchronization verification passed. The test note is synchronized at vault
commit `eb2647e7802bad86a9ed18aa5fbafbc83f45269c`. An Obsidian client pull
remains unverified. See [acceptance evidence](issues/04-verify-and-activate-vault-editing.md).

The subsequent plain-language preview and saved-file-approval refinement in
[ticket 05](issues/05-simple-previews-and-file-permissions.md) is implemented
locally; deployment and its live WhatsApp acceptance remain pending.

## Goal

Give Jarvis one bounded capability for updating ordinary Obsidian Markdown notes
and synchronizing those updates through the vault's Git remote. The operator
should be able to ask Jarvis to change a note, inspect the exact diff, approve
once, and receive a precise result describing whether the change was committed
and whether the commit reached the remote.

The capability belongs in the personal assistant runtime beside `read_vault`.
It does not require an Obsidian plugin, an MCP service, or a second worker. The
configured Ubuntu clone is the only clone Jarvis edits. Obsidian clients on
other devices continue to synchronize through the same Git remote.

## Configuration boundary

The existing `vault_path` setting remains a read-only vault root when no
`[vault_git]` section is present. This preserves existing deployments and their
`read_vault` behavior.

Write-capable synchronization is opt-in. A `[vault_git]` section requires
`vault_path` and configures the bounded Git operation with:

- `remote` and `branch` for the dedicated clone;
- `note_directories` for the allowed Markdown subdirectories;
- `author_name` and `author_email` for the Jarvis commit identity; and
- `ssh_identity_file` and `ssh_known_hosts_file` for the repository-specific
  SSH authentication and pinned host verification.

The vault clone is outside the application repository and is owned by the
service account. Git is invoked through an argument list with a controlled
environment; the capability never asks `run_terminal` to execute Git, never
uses a shell, and never exposes the SSH key or remote credentials to the model.

## Read contract

When `[vault_git]` is configured, a fresh vault request first fetches the
configured remote and fast-forwards the clean dedicated clone. `read_vault`
returns the content together with the full base revision and freshness state
needed to prepare a write. If the remote is unavailable, a clean last-known
clone may serve a read only when the result clearly says it is stale. A write
proposal requires a successful synchronized base.

Dirty state, a non-fast-forward update, an unexpected branch, or a repository
layout that cannot be proved to be the configured clone stops the operation.
Jarvis does not merge, rebase, or resolve vault conflicts automatically.

## `edit_vault` contract

`edit_vault` accepts a batch against the revision returned by `read_vault`:

```json
{
  "base_revision": "full-git-commit-sha",
  "commit_message": "jarvis: update project status",
  "changes": [
    {
      "operation": "update",
      "path": "Projects/Jarvis.md",
      "replacements": [
        {
          "old_text": "Status: planning",
          "new_text": "Status: implementation"
        }
      ]
    },
    {
      "operation": "create",
      "path": "Ideas/Voice capture.md",
      "content": "# Voice capture\n\nCapture quick thoughts through Jarvis.\n"
    }
  ]
}
```

The tool supports `update` and `create` in the first version. It does not delete
or rename notes. Every path is canonicalized relative to `vault_path` and must
be an ordinary `.md` file below one of the configured `note_directories`.
Traversal, absolute paths, symlinks, junctions, hidden directories, `.git`,
`.obsidian`, non-Markdown files, and files outside the configured note
directories are rejected.

For an update, every `old_text` must match exactly once in the note as it was
read. Zero matches, multiple matches, overlapping replacements, an existing
create path, invalid UTF-8, or a result outside configured size limits rejects
the entire batch. The tool preserves the rest of the note, including
frontmatter, formatting, and line endings. It never guesses an edit location.

The tool synchronizes and validates the base, computes the complete resulting
files and internal unified diff, validates the commit message, and freezes the
exact proposal. The operator sees the file and plain-language Add, Remove,
Replace/With, or Create sections containing the actual changed text. Git diffs,
revisions, hashes, and commit messages stay out of the preview and final reply.
Oversized previews are rejected or split before approval, never truncated.

## Approval and execution

The proposal is one pending action. It uses the existing runtime continuation
and accepts `1` to approve once, `9` to reject, and `/cancel`. A single-file
proposal also offers `2` to approve and remember future edits to that exact file.
The saved permission is scoped to the canonical note path, clone, remote, and
branch, persists across restarts, and is independent of terminal permissions.
Use `/permissions` to list grants and `/forget-permission ID` to revoke one.
Multi-file proposals offer only one-time approval unless every changed file
already has a saved grant. Jarvis uses one file per call to offer choice `2`.
The approved patch, paths,
base revision, and commit metadata are frozen inside the continuation. A later
model message cannot replace the patch after approval.

The runtime shows the preview and waits outside the model loop. When the loop
resumes, the result explicitly reports the operator's approval or rejection
or the use of a saved file permission, so Jarvis can describe the decision accurately.

After `1`, `2`, or a matching saved file permission, the tool takes the vault
lock, fetches again, and verifies that the
branch, local state, remote base, and approved base revision still match. It
then:

1. applies the frozen changes;
2. verifies that the resulting working-tree diff is exactly the approved diff;
3. stages only the approved note paths;
4. creates one commit with the configured Jarvis identity; and
5. pushes that commit normally, without force or history rewriting.

The lock covers local Git and file operations, not the time spent waiting for
operator approval. Revalidation protects the gap between preview and
execution. Any change invalidates the proposal before it writes.

The tool distinguishes these outcomes:

| Outcome | Meaning |
| --- | --- |
| `no_change` | The requested result equals the current note; no commit was made. |
| `rejected` / cancellation | The operator rejected or cancelled the proposal; no note was changed. |
| `stale_base` | The approved revision is no longer current; a new read and proposal are required. |
| `synced` | The exact commit was created and verified at the configured remote. |
| `committed_not_synced` | The commit exists locally, but the remote rejected it or could not accept it. |
| `sync_unknown` | The push outcome was uncertain; remote verification was attempted but could not prove the commit. |
| Execution failure | The result identifies any local changes left behind; dirty or divergent Git state prevents another write until an operator repairs it. |

An uncertain push is checked against the remote before the result is returned.
Jarvis never blindly retries an uncertain operation, force-pushes, merges,
rebases, cherry-picks, rewrites history, or resolves a conflict autonomously.
A local commit is preserved when it exists so an operator can recover it.

## Work packages

The implementation and subsequent approval refinement are tracked in these tickets:

1. [Configure the Git-backed vault and synchronized reads](issues/01-configure-vault-git-and-synchronized-reads.md)
2. [Prepare exact Markdown vault edits](issues/02-prepare-exact-markdown-vault-edits.md)
3. [Approve, commit, and synchronize one vault edit](issues/03-approve-commit-and-synchronize-vault-edits.md)
4. [Verify and activate vault editing](issues/04-verify-and-activate-vault-editing.md)
5. [Simple edit previews and remembered file approval](issues/05-simple-previews-and-file-permissions.md)

Ticket 04 records automated temporary-clone tests, deployment validation,
and authorized live WhatsApp acceptance against the configured vault remote.
Client-side Obsidian synchronization remains a separate, unverified check.

## Out of scope

- Editing through the general terminal capability.
- An Obsidian plugin or official Obsidian Sync integration.
- Deleting, renaming, moving, or editing non-Markdown vault files.
- Editing `.obsidian`, hidden paths, Git metadata, or non-Markdown files.
- Automatic merge, conflict resolution, force-push, retry after an uncertain push, or host failover.
- A blanket permission for future vault writes.


