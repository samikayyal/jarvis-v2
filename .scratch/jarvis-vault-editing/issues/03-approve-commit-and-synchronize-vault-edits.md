Type: task
Status: complete
Blocked by: 01, 02

# Approve, commit, and synchronize one vault edit

## Question

How should the runtime turn one frozen `edit_vault` proposal into exactly one
reviewed commit and a truthful synchronization result?

## Scope

- Reuse the existing pending-action continuation and present the complete
  affected paths, base revision, diff, commit message, and the statement that
  approval writes, commits, and pushes this exact batch.
- Accept only the exact one-time choices `1`, `9`, and `/cancel`. Do not offer
  or save a terminal-style permission for vault edits. Rejection, cancellation,
  or service restart discards the unexecuted proposal.
- On approval, acquire the vault lock, fetch again, and verify the configured
  branch, clean state, remote base, and approved `base_revision`. Abort before
  writing if any part changed and return `stale_base` or a manual recovery
  result.
- Apply only the frozen changes, verify the resulting working-tree diff equals
  the approved diff, stage only the approved Markdown paths, commit once with
  the configured Jarvis identity, and push normally without force or history
  rewriting.
- Verify a push outcome when it is uncertain. Never automatically retry an
  operation after a timeout or transport failure whose remote effect is
  unknown. Preserve an existing local commit and report it as not synchronized
  when the remote has not accepted it.
- Return a structured result that distinguishes `no_change`, rejection/cancellation,
  `stale_base`, `synced`, `committed_not_synced`, `sync_unknown`, and
  execution failures with local changes. Block later writes when local Git state requires
  operator recovery.

## Acceptance criteria

- [x] One approval is sufficient for one exact batch and no saved permission can
  approve a later vault edit.
- [x] A branch, remote, working-tree, or base-revision race is detected after
  approval before any proposed file is written.
- [x] The post-apply diff is independently compared with the frozen proposal;
  a mismatch stops before commit or push.
- [x] Only approved note paths are staged and one commit is created with the
  configured author and a bounded `jarvis:` commit subject.
- [x] Normal push success returns the commit ID and `synced` outcome.
- [x] Rejected pushes preserve the local commit and return
  `committed_not_synced`; ambiguous pushes perform remote verification and
  return either `synced` or `sync_unknown` without a blind retry.
- [x] Merge, rebase, cherry-pick, force-push, history rewrite, conflict
  resolution, and arbitrary terminal Git execution are impossible through the
  prepared tool.
- [x] Cancellation and restart leave the proposed clone unchanged and do not
  leave a pending action that can resume later.

## Comments

- The lock covers local file and Git operations only. The approval wait is
  protected by the post-approval revalidation instead of holding the lock.

Implementation and focused verification completed on 2026-10-04. Real temporary Git repositories cover the read/edit/approval/commit/push contract; configuration, Responses continuation, rejection, stale reads, base races, CRLF, empty-note creation, and uncertain push outcomes pass.

