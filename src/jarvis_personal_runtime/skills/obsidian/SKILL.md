---
name: obsidian
description: Find, read, create, edit, or delete Obsidian notes and browse vault folders and files.
---

Use the prepared vault tools for all vault operations, including listings and Git-backed changes. Never substitute terminal commands for these operations.

## Find and read

Use `list_vault_tree` to discover paths without reading contents; use `read_vault` for note contents. Start a tree listing at offset 0 and follow `next_offset` until null before claiming a complete listing. Restart at 0 if the base revision changes between pages. The tree excludes hidden metadata, symlinks, and nested repositories. Disclose stale or incomplete results.

## Edit and delete

Read an existing note before editing, preserve unrelated content, and prepare exact Markdown changes below the configured note directories using `edit_vault`. Use one file per proposal to offer per-file approval. The runtime handles 1 (approve once), 2 (remember approval for this exact file), or 9 (reject). Report saved approval only when the result confirms it.

For whole-note deletion, read the exact path and synchronized base revision first, then use `delete_vault`. The runtime previews the full note and offers only 1 (approve once) and 9 (reject). Never offer option 2 for deletion; saved edit permissions do not apply. Git history is retained. A truncated preview cannot authorize a write.

Editing and deletion are available only when their tools are exposed. A read-only vault stays read-only.

## Report the outcome

Distinguish `synced`, `committed_not_synced`, and `sync_unknown`. Claim remote synchronization only when verified. Explain failed or uncertain synchronization plainly; never blindly retry an uncertain push.

Briefly describe the change and sync outcome, for example: "Added the checklist to Jarvis.md. Saved and synced." If confirmed by the result, add "Future edits to this file are approved." Keep hashes, revisions, internal diffs, and commit messages out of replies unless requested.
