# 03: Integrate the complete workflow and remove the old machinery

**What to build:** Ordinary Jarvis web-assisted requests use the complete concurrent-reader workflow, show useful progress during lengthy research, and finish with concise evidence-aware replies. The superseded reader workflow is removed rather than retained underneath a new interface.

**Blocked by:** 02 — Add concurrent readers and retained follow-ups.

**Status:** claimed

- [ ] Normal main-agent tool discovery and runtime instructions expose reader start, collection/status, wait, follow-up, cancellation and release coherently. A complete model-and-tool scenario demonstrates delegation, other main work, result synthesis and final cleanup.
- [ ] Provide a brief factual progress message after roughly 20 seconds if processing continues, then only on meaningful developments. Progress reflects observable work, does not narrate each action, and stops on cancellation, failure or final completion without racing a late message.
- [ ] Preserve clear busy handling for new ordinary WhatsApp messages during an active request. `/status` and `/cancel` remain responsive during concurrent reader work. Do not add message steering or a user-request queue.
- [ ] Final replies respect requested length and contain relevant uncertainty in ordinary language. No code-owned source-warning paragraphs are appended after the model's answer.
- [ ] Remove superseded reader quote validation, mandatory fallback searches, forced chunk sweeps, coverage accounting, required findings schema and reader action/page counters. Optional oversized-content search/reading remains available without reviving mandatory scanning.
- [ ] Remove obsolete configuration and tests for deleted policy, with clear handling of existing deployed configuration. Retain protections, execution limits and main web-search behavior that still apply.
- [ ] Update the domain glossary, architectural decisions, runtime web instructions and operational documentation to describe the final implementation. Explicitly supersede conflicting earlier reader-design decisions without claiming deployment or successful live acceptance prematurely.
- [ ] Run focused integration and regression checks covering main/reader coordination, screenshots, dynamic observations, overflow, follow-ups, progress, busy handling, cancellation and cleanup. Run the project's required broader checks before declaring the candidate ready for deployment.
- [ ] Verify that existing vault-edit approval/sync behavior and main search are preserved. Vault-edit redesign remains outside this ticket even if it is a remaining latency bottleneck.
- [ ] Record a reproducible predeployment verification result for the exact candidate, including reader timing/model-call instrumentation needed for live comparison. Do not activate the candidate as part of this ticket.
