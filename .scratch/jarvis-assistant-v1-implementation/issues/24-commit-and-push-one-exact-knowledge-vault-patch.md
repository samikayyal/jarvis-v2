# 24 — Commit and push one exact knowledge-vault patch

**What to build:** One Markdown-only vault proposal freezes its remote base, canonical paths, complete diff, and commit metadata, then applies, verifies, commits, and normally pushes exactly that change after approval.

**Blocked by:** 08 — Present oversized proposals through the universal envelope; 23 — Read and search the synchronized knowledge vault.

**Status:** complete

- [x] The proposal includes the exact remote base, canonical allowed Markdown paths, complete unified diff, and configured commit identity and message.
- [x] Dispatch re-fetches and verifies a clean unchanged base before applying and independently verifying exactly the approved diff.
- [x] Dirty state, changed base, non-fast-forward push, conflict, excluded path, rename, deletion, merge, rebase, history rewrite, and force-push stop without autonomous recovery.

## Implementation evidence

- Added the approval-frozen Markdown-only write proposal, bounded canonical path/content validation, exact staged-diff verification, configured Jarvis commit metadata, normal push, retry-only transient push handling, and manual-recovery blocking after commit ambiguity.
- Wired the write connector through the orchestration proposal boundary and routed broker dispatch/cancellation/finalization without giving the model write authority.
- Added contract coverage for path and nested-repository exclusions, base races, dirty state, staged mismatch, trailing-space and diff-header exactness, operation mismatch, push conflicts, broker dispatch, and the subprocess Git edge.
- Validation: `uv run pytest -q` — 368 passed; `uv run ruff check .`; `uv run ruff format --check src tests`; `uv run python -m compileall -q src tests`; `git diff --check`.

