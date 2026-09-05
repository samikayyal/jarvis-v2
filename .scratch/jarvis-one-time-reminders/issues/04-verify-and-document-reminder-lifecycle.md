# 04 — Verify and document the complete Reminder lifecycle

**What to build:** Deliver a coherent, reviewable Reminder capability whose configuration, operational behavior, trust boundaries, and verification evidence match the approved spec from creation through terminal delivery or cancellation. Provide a supervised real-phone acceptance procedure without expanding this ticket into production activation or a general automation framework.

**Blocked by:** 03 — Edit and cancel pending reminders.

**Status:** ready-for-agent

- [ ] Review the completed implementation against every user-facing requirement and implementation decision in the approved Reminder spec, repairing only gaps within that scope.
- [ ] Ensure domain, configuration, deployment, and operational documentation describe the proactive Reminder exception, the configured operator timezone, the SQLite runtime state, the one-attempt delivery contract, and the Jarvis-versus-OpenWA ownership boundary consistently.
- [ ] Ensure example configuration contains the non-secret Reminder settings and that normal configuration checking validates them without writes or network access.
- [ ] Verify no implementation path can select a third-party recipient, split a body, call the model at delivery time, or schedule anything other than a one-time Reminder to the authorized operator.
- [ ] Verify no recurrence, snoozing, contact lookup, Google Calendar dependency, external scheduler, queue, new resident service, or new Python package was introduced.
- [ ] Verify terminal Reminder records are retained, excluded from delivery, pending-only listing remains the default, and explicit historical listing reports `sent`, `failed`, `unknown`, and `cancelled` accurately.
- [ ] Verify all Reminder mutations use exact `1` or `9` non-saveable approval and cannot be changed between preview and commit.
- [ ] Verify runtime traces provide bounded evidence for proposals, committed mutations, attempts, accepted outbound IDs, failures, and ambiguous outcomes under the existing sensitive-data rules.
- [ ] Run the focused Reminder and directly affected regression tests first with `uv`.
- [ ] Run the repository’s Ruff check, formatting check, and Python compilation gates with `uv` after focused tests pass.
- [ ] Run the full test suite exactly once at the end with a sufficiently long timeout and report the exact result without treating a timeout as success.
- [ ] Document one supervised acceptance procedure that creates a near-future Reminder from the authorized WhatsApp account, verifies its exact approval preview, optionally edits its body or Due time, and confirms the approved body is received once on the operator’s phone.
- [ ] Make the acceptance procedure explicitly human-owned for sending the WhatsApp requests and confirming phone receipt; instructions alone must not be presented as executed evidence.
- [ ] Preserve the explicit exclusions for downtime recovery, late delivery, missed-reminder handling, edit-versus-send races, and ordering with simultaneous ordinary replies.
- [ ] Confirm the final diff is limited to the Reminder capability and its documentation, with unrelated working-tree changes preserved.
