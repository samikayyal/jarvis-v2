Type: task
Status: complete
Blocked by: 03

## Question

Run focused connection, operation-bound, failure, approval, reconnection,
manifest-drift, and regression tests, followed by the one final surviving full
suite, to prove the complete Google MCP implementation before any live
authorization or production change.

## Answer

Proved the complete supportable Google MCP contract without live authorization
or production changes. The existing public-seam tests cover exact connection
commands, selected-operation exposure, bounded reads, normalized failures,
one-attempt Calendar writes, approval and rejection behavior, terminal-only
`2`, connection replacement and disconnection invalidation, OAuth endpoint
pinning and refresh, and fail-closed manifest drift.

Evidence captured on 2026-09-03:

- Focused Google contract and runtime composition: `73 passed`.
- OpenWA, Responses, terminal, vault, trace, deduplication, permissions, and
  packaging regressions: `99 passed, 1 skipped`.
- Ruff check, Ruff format check, and Python compilation: passed.
- One final repository-wide suite: `172 passed, 1 skipped`.
- Final two-axis review found no runtime/spec defect or scope creep. Its
  canonical-documentation findings were repaired in `CONTEXT.md` and the local
  issue-tracker guidance.

The skipped case is the existing native Ubuntu process-group test, which is
platform-conditionally skipped on Windows.
No production code change was required: the verified implementation already
satisfies the currently supportable official contract. Gmail remains read-only
because Google's official hosted Gmail MCP service still exposes no send or
reply operation; that upstream limitation is not bypassed by this proof.

## Comments

- Completed before any live Google consent, credential installation, remote
  write, service restart, or production activation.
