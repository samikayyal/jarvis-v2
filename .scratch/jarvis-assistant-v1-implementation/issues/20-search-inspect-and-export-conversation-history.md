# 20 — Search, inspect, and export accessible conversation history

**What to build:** Authorized inbound and Jarvis outbound text is retained verbatim by working-session conversation and can be deterministically searched, inspected, exported, and selectively reused with provenance disclosure.

**Blocked by:** 14 — Orchestrate natural-language requests through the Agents SDK.

**Status:** ready-for-agent

- [ ] Authorized text history is immutable, correlated to its conversation and request, and searchable through bounded local full-text and deterministic filters.
- [ ] Inspection and export preserve exact selected content and disclose when retrieved history informed a response.
- [ ] Credential-like records remain searchable but are excluded from automatic retrieval and model context unless the operator selects them exactly.

