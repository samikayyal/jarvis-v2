---
status: superseded
---

# Enforce page reader fallback and measured coverage

Superseded in the repository by [ADR 0005](0005-simplify-and-parallelize-page-readers.md)
and ticket 03 on 2026-11-07. The enforced quote/fallback/coverage policy and its
configuration were removed. The text below records the historical decision;
it is not the current implementation contract. Deployment remains separate.

The page-reader coverage design retains the isolated, session-retained reader of
[ADR 0003](0003-isolate-webpage-reading.md) and adds local full-text search returning
chunk IDs. When a question remains unresolved, code requires bounded related-term
search and matching-chunk inspection, relevant display review, then remaining-chunk
inspection in capture order. The model still chooses search terms and relevant
display actions; code validates source quotes and preserves measured coverage and
source-specific disclosures. This deliberately adds orchestration so an early
"not found" cannot skip fallback, while keeping one retained conversation and no
per-question-part checklist. Coverage does not prove semantic comprehension or
absence from the entire website, and resource limits can prevent exhaustive reading.

The operator confirmed implementation with 24 model turns, 18 reader actions,
120 seconds and 128,000 context tokens. The
[specification](../../.scratch/page-reader-coverage/spec.md) records the final
alignment and supersedes earlier interview alternatives.
