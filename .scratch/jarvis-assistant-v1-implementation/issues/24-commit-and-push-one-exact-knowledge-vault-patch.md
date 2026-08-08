# 24 — Commit and push one exact knowledge-vault patch

**What to build:** One Markdown-only vault proposal freezes its remote base, canonical paths, complete diff, and commit metadata, then applies, verifies, commits, and normally pushes exactly that change after approval.

**Blocked by:** 08 — Present oversized proposals through the universal envelope; 23 — Read and search the synchronized knowledge vault.

**Status:** complete

- [x] The proposal includes the exact remote base, canonical allowed Markdown paths, complete unified diff, and configured commit identity and message.
- [x] Dispatch re-fetches and verifies a clean unchanged base before applying and independently verifying exactly the approved diff.
- [x] Dirty state, changed base, non-fast-forward push, conflict, excluded path, rename, deletion, merge, rebase, history rewrite, and force-push stop without autonomous recovery.

## Implementation evidence

- Added the approval-frozen Markdown-only write proposal, bounded canonical path/content validation, exact staged-diff verification, configured Jarvis commit metadata, normal push, and manual-recovery blocking after an unknown push outcome.
- Kept the Agents SDK orchestration adapter non-authoritative: it emits only a typed path-to-new-content intent, while the deterministic broker invokes the narrow proposal-preparation port and freezes the connector-produced base, diff, and metadata before approval.
- Classified the subprocess Git push edge into explicit pre-dispatch failure, repository conflict, and unknown outcome categories; only a provably pre-dispatch failure receives one bounded same-commit retry.
- Added contract coverage for path and nested-repository exclusions, base races, dirty state, staged mismatch, trailing-space and diff-header exactness, operation mismatch, push conflicts, broker preparation/dispatch, ambiguous timeout and generic push failures with no retry, and the subprocess Git edge.
- Validation: `uv run pytest -q` — 377 passed; `uv run ruff check .`; `uv run ruff format --check src tests`; `uv run python -m compileall -q src tests`; `git diff --check`.

