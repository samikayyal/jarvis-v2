Type: task
Status: complete
Blocked by: 01

# Prepare exact Markdown vault edits

## Question

What strict `edit_vault` input and diff-preparation contract lets Jarvis make
small, reviewable note changes without asking the model to construct Git
patches or choose arbitrary filesystem paths?

## Scope

- Expose one `edit_vault` prepared tool only when the Git-backed vault is
  configured. Its batch includes `base_revision`, `commit_message`, and
  `changes`.
- Support `update` changes made from exact text replacements and `create`
  changes containing complete UTF-8 Markdown content. Keep one batch as one
  future commit.
- Require every update replacement to match exactly once in the original note;
  reject zero matches, multiple matches, overlaps, invalid content, and
  unchanged/no-op batches. Preserve all unrelated bytes and existing
  frontmatter/formatting/line endings.
- Canonicalize paths below `vault_path` and allow only ordinary `.md` files in
  configured `note_directories`. Reject traversal, absolute or platform-specific
  paths, symlinks, junctions, hidden directories, `.git`, `.obsidian`,
  non-Markdown files, deletes, and renames.
- Synchronize and validate the supplied `base_revision` before preparing the
  proposal. Compute the complete resulting files and unified diff in memory or
  an isolated temporary area; do not modify the dedicated clone while waiting
  for approval.
- Validate a bounded one-line commit message and freeze the canonical paths,
  base revision, resulting content, diff, and commit metadata in the pending
  continuation. The preview must show the complete diff or reject the proposal
  as too large to review.

## Acceptance criteria

- [x] The Responses schema is strict and rejects unknown fields, invalid
  operations, missing fields, duplicate paths, and out-of-bound content.
- [x] Exact replacement behavior is deterministic: one match succeeds, zero or
  multiple matches fail with a useful reason, and overlapping replacements are
  rejected without partial writes.
- [x] New-note creation rejects an existing path and accepts only configured
  ordinary Markdown directories.
- [x] Traversal, absolute paths, symlinks, hidden/excluded paths, non-Markdown
  files, deletes, renames, and changes outside note directories are rejected.
- [x] The prepared result contains the full base revision, canonical affected
  paths, complete unified diff, and commit metadata needed for approval.
- [x] Preparing a proposal does not write the clone, create a commit, push, or
  create a saved permission.
- [x] The proposal remains bound to the exact `read_vault` revision and cannot
  be changed by a later model message.

## Comments

- This ticket deliberately keeps edit intent at the text/content level. Git
  staging and synchronization belong to the execution ticket.

Implementation and focused verification completed on 2026-10-04. Real temporary Git repositories cover the read/edit/approval/commit/push contract; configuration, Responses continuation, rejection, stale reads, base races, CRLF, empty-note creation, and uncertain push outcomes pass.

