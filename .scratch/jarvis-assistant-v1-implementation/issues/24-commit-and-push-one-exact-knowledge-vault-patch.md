# 24 — Commit and push one exact knowledge-vault patch

**What to build:** One Markdown-only vault proposal freezes its remote base, canonical paths, complete diff, and commit metadata, then applies, verifies, commits, and normally pushes exactly that change after approval.

**Blocked by:** 08 — Present oversized proposals through the universal envelope; 23 — Read and search the synchronized knowledge vault.

**Status:** ready-for-agent

- [ ] The proposal includes the exact remote base, canonical allowed Markdown paths, complete unified diff, and configured commit identity and message.
- [ ] Dispatch re-fetches and verifies a clean unchanged base before applying and independently verifying exactly the approved diff.
- [ ] Dirty state, changed base, non-fast-forward push, conflict, excluded path, rename, deletion, merge, rebase, history rewrite, and force-push stop without autonomous recovery.

