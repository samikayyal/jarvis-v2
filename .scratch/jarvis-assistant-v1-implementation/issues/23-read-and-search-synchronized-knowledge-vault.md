# 23 — Read and search the synchronized knowledge vault

**What to build:** Operator requests synchronize a controlled dedicated clone when possible and perform bounded deterministic knowledge-vault reads and searches with clear stale-read disclosure.

**Blocked by:** 14 — Orchestrate natural-language requests through the Agents SDK.

**Status:** complete

- [ ] A clean clone fetches and fast-forwards before fresh reads; unavailable synchronization permits only a clean stale read with visible age and warning.
- [ ] Bounded path, filename, Markdown, tag, frontmatter, and link-aware searches return referenced excerpts through the control-plane seam.
- [ ] Traversal, symlinks, hidden/excluded areas, external indexing, semantic retrieval, and arbitrary filesystem access are rejected.

