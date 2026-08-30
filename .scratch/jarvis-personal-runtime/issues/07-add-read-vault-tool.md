Type: task
Status: complete
Blocked by: 05

## Question

Implement `read_vault` as one prepared read-only tool with bounded `search` and exact-Markdown-file `read` modes rooted at the configured vault directory, rejecting traversal, non-Markdown targets, writes, and oversized results.

Expose the smallest stable tool schema needed by the direct Responses loop and prove it with focused filesystem and tool-contract tests.

## Answer

Added one `read_vault` prepared tool to the fresh personal runtime. Its strict
Responses schema exposes only `mode` (`search` or `read`) and `value`; configured
runtime composition includes it when `vault_path` is set.

Exact reads accept only canonical vault-relative POSIX `.md` paths and preserve
the complete UTF-8 file content. Traversal, absolute and Windows paths, hidden or
symlinked paths, non-Markdown files, missing files, invalid UTF-8, write modes,
extra arguments, and oversized files or serialized results are rejected.

Search is deterministic and case-insensitive across ordinary Markdown paths and
content. It returns at most eight bounded excerpts while enforcing query, note,
per-note byte, total scanned byte, and final result limits. Focused filesystem,
configuration, strict-schema, and direct Responses continuation tests cover the
contract.

## Comments

- Completed on 2026-08-30 with focused personal-runtime tests, Ruff, formatting,
  and compile checks passing before the final repository-wide gate.
- The single final full-suite run completed with 890 passed and 2 skipped. Its
  four Ticket 27 failures all reported `application source differs from the
  pinned artifact`: the immutable legacy deployment lock covers all `src/**/*.py`,
  while Issue 02 explicitly prohibits repinning that rollback bundle around the
  in-progress replacement runtime.
