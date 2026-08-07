# 22 — Manage explicit durable assistant memory

**What to build:** Only explicit remember instructions create durable assistant memory, and exact confirmed replacement or forgetting affects only the selected inspectable record.

**Blocked by:** 08 — Present oversized proposals through the universal envelope; 20 — Search, inspect, and export accessible conversation history.

**Status:** complete

- [x] No inferred preference or ordinary conversation automatically creates durable memory.
- [x] Memories are deterministically inspectable with provenance until exact confirmed replacement or forgetting.
- [x] Credential-like memories remain plaintext and inspectable but are excluded from automatic retrieval and model context unless explicitly selected.

## Implementation evidence

- Added explicit `remember` and `/memory` read/write paths with approval-gated durable mutations.
- Added SQLite and in-memory lifecycle persistence with stable IDs, provenance, exact revision checks, and content-free terminal records.
- Added automatic safe-memory selection with credential-like exclusion and metadata-only memory audit events.
- Validation: `uv run pytest -q` (360 passed), `uv run ruff check .`, and `uv run ruff format --check .`.
