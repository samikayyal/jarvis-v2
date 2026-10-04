Type: task
Status: claimed
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
- [ ] Focused and full tests, static checks, deployment validation, and a live
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
- Live deployment and WhatsApp acceptance are pending because the Tailscale
  route to the deployment host is unavailable. Network diagnosis is in progress;
  the production service has not been replaced by this refinement.
