# 04 — Retain complete diagnostic traces and enforce trace capacity

**What to build:** Model, Codex, connector, and worker operations retain complete payload traces indefinitely in the manual-administration boundary, including credential-like material, and new trace-producing work is admitted only when its trace can be retained.

**Blocked by:** 01 — Establish the signed-message control-plane tracer bullet.

**Status:** ready-for-agent

- [ ] Controlled operations retain complete inputs, outputs, arguments, results, errors, and credential-like payloads without automatic redaction or expiry.
- [ ] Trace content is unavailable to ordinary Jarvis behavior and accessible only through the manual-administration boundary.
- [ ] Insufficient capacity rejects new trace-producing work without deleting or truncating previously retained traces.

