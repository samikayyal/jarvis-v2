# 04: Deploy and verify speed and answer quality

**What to build:** Deploy the completed concurrent-reader release through the repository's GitHub synchronization contract, then verify the original hosting-comparison workflow visibly in WhatsApp with a saved Obsidian note and measured speed and answer quality.

**Blocked by:** 03 — Integrate the complete workflow and remove the old machinery.

**Status:** claimed

- [ ] Inspect current checkout, remote and live-release state. Commit and push intended candidate changes and verify that GitHub contains the exact candidate SHA before activation. Transferred source must match that verified commit; failed push or verification leaves the active release unchanged.
- [ ] Provision Playwright browser binaries and OS dependencies reproducibly on Ubuntu. Verify the staged exact-candidate package, configuration compatibility, browser startup, model screenshot capability and required tests before activation; retain a working rollback path.
- [ ] Activate the verified candidate and check runtime service health, OpenWA health and the exact configured named-session readiness. Record the active commit and installed-source verification rather than relying only on process health.
- [ ] Through the visible authorized Jarvis WhatsApp conversation, repeat: "Should I keep hosting you on my Ubuntu laptop or move you to GCP? Save the full result in an Obsidian note. Keep your final WhatsApp summary short." Correlate visible replies with runtime traces.
- [ ] Verify the note was actually saved and synced, the recommendation and numeric claims are supported by inspected sources, material gaps are specific, and the WhatsApp final reply is concise without generic warning appendices. Do not treat removal of production quote validation as evidence of factual correctness.
- [ ] Observe the concurrent-reader path, meaningful progress and browser lifecycle. Exercise live follow-up/cancellation as appropriate and verify no late replies or leftover reader browsers/workers. Keep end-to-end acceptance distinct from health checks and unit-test success.
- [ ] Report total time from admission through completion and delivery, main and reader model-call counts, and available component timings against the baseline of 458 seconds before completion, 22 main calls and 27 reader calls. Do not double-count reader model time inside reader wall time.
- [ ] Assess the initial under-two-minute target for the complete hosting-and-note scenario. If missed, record actual results, remaining bottlenecks and follow-up work; do not claim the speed problem solved or expand into vault-edit redesign incidentally. Distinguish functional acceptance from performance-target attainment.
- [ ] Commit and push resulting deployment/acceptance documentation and verify GitHub is current. Record incomplete live checks or unavailable browser-control access honestly; do not substitute infrastructure checks for visible acceptance.

## Completion interpretation

This ticket delivers a verified deployment and an evidence-backed acceptance result. Missing the latency target is a reported performance outcome, not permission to describe it as met. A completed deployment alone does not satisfy the live acceptance criteria.
