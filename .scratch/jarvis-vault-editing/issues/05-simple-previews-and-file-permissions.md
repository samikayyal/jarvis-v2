Type: task
Status: complete
Blocked by: 04

# Simple edit previews and remembered file approval

## Scope

The operator requested plain-language previews for additions, removals,
replacements, and note creation, plus `1` to approve once, `2` to approve
future edits to that exact file, and `9` to reject. Successful replies should
briefly describe the edit and synchronization without Git hashes or commit
messages. This supersedes the one-time-only approval and Git preview requirements
in tickets 02 and 03. Removing text from a note is supported; whole-file deletion
remains outside this tool's contract.

## Acceptance criteria

- [x] Previews render the actual frozen content changes in simple language.
- [x] `2` persists permission for exactly one canonical note in the same clone,
  remote, and branch; subsequent edits survive restart without another prompt.
- [x] `1`, `9`, and `/cancel` never save permission. Persistence failure leaves
  the action pending and executes nothing.
- [x] Another file, clone, remote, or branch cannot inherit the grant. Multi-file
  batches require one-time approval unless every affected file already has a grant.
- [x] `/permissions` lists file grants and `/forget-permission` revokes them.
- [x] Exact text, clean-tree, synchronized-base, and push verification gates still apply.
- [x] Git metadata remains in diagnostic traces; user-facing replies stay simple
  and distinguish successful synchronization from failures or uncertain outcomes.
- [x] Focused and full tests, static checks, deployment validation, and a live
  WhatsApp preview/approval/reply check pass.

## Comments

Implementation authorized as a continuation of the deployed vault tools.

## Verification — 2026-10-04

- Real temporary Git clones and a bare remote verify the whole runtime's `2`
  approval, persisted grant across a new runtime, automatic synchronization,
  permission listing/revocation, and rejection after revocation.
- Focused coverage proves other approval choices do not save grants, a save
  failure leaves the proposal pending, and different files/clones/remotes/branches
  do not inherit permission. Saved grants still enforce exact text, synchronized
  bases, and clean working trees. Add/remove/blank-line previews are covered.
- Full suite: 305 passed, 1 skipped in 314.52 seconds. Ruff, formatting,
  compilation, and diff checks passed.
- Activated release `40184830fd90bfc3a22967a94949232014e6e5c7` after
  service-account candidate `--check` passed. The final display correction has
  5 focused passing tests; the full suite above ran on the immediately preceding
  implementation. Ruff, formatting (152 files), compilation, and diff checks passed.
- The first live append preview exposed a missing-final-newline case that showed
  a replacement. The final release fixes this and the live preview now says
  `Add: Simple approval test: added.` with the requested `1`/`2`/`9` choices.
- WhatsApp `2` approved and synced the addition, then replied: "Added ... Saved
  and synced. Future edits to this file are approved." A removal request then
  completed without another approval and replied simply that it was saved and synced.
- Independent service-account verification proved both outcomes were `synced`:
  first `approved_and_saved` with a preview, then `saved_file_permission` without
  a preview. Local HEAD and remote main matched at
  `f958d3cfc958414539c87028466a3d4c658629b0`, and the clone was clean.
- The test note was restored byte-for-byte to SHA-256
  `4aa3ba012e2823042c7b24367592d8cb11050df8a0d407878bbb834e6568466a`.
  `/forget-permission cbe82a8c6541a067` returned "Permission removed." A subsequent
  request to remove `Status: verified` showed a fresh `Remove:` preview and the
  approval choices, confirming revocation. Earlier `9` rejection was acknowledged
  and independently verified to leave the note unchanged.
- Activation health passed: service active/enabled, zero restarts, exact listener
  `172.20.0.1:9011`, OpenWA healthy, configured named session `ready`.
  Rollback release: `3bbff5d2cb97ede2a97189ebca6580766e62a212`; prompt backup:
  `/etc/jarvis/personal-runtime-backups/vault-simple-1791127836`.
- At final cleanup, connectivity dropped again. WhatsApp shows "Computer not
  connected" and the final removal-test `9` is queued, not yet acknowledged;
  no approval was sent for that proposal. Final idle status and a post-revocation
  host health check remain unverified. An Obsidian client pull is also unverified.
