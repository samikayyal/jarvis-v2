# 03 — Edit and cancel pending reminders

**What to build:** Let the authorized operator safely change or cancel a pending Reminder. The complete slice must preserve deterministic IDs and approval semantics, recalculate the scheduler after an approved mutation, reject ambiguous or terminal targets, and leave state untouched when the operator rejects a proposal.

**Blocked by:** 02 — Send each due reminder once.

**Status:** ready-for-agent

- [ ] Expose `edit_reminder` as a prepared operation accepting a stable Reminder ID and at least one of a replacement body or replacement strict local due time.
- [ ] Support body-only, time-only, and combined edits while preserving the stable Reminder ID.
- [ ] Validate the complete resulting Reminder using the same body, timezone, local-time, and future-time rules as creation.
- [ ] Show the complete resulting body, canonical local date, time, timezone, and stable ID in a frozen non-saveable approval proposal.
- [ ] Apply an approved edit exactly once, wake the scheduler, and recalculate the earliest pending Due time.
- [ ] Leave the existing Reminder byte-for-byte unchanged when an edit is rejected or its proposal expires before approval.
- [ ] Expose `cancel_reminder` as a prepared operation accepting a stable pending Reminder ID.
- [ ] Show the exact body, canonical local date, time, timezone, and stable ID before cancellation and require the same exact non-saveable approval grammar.
- [ ] Mark an approved cancellation terminal, retain it for historical listing, wake the scheduler, and ensure it is never selected for delivery.
- [ ] Leave a rejected cancellation pending and unchanged.
- [ ] Reject missing Reminder IDs and reject edits or cancellations of `sent`, `failed`, `unknown`, or `cancelled` records.
- [ ] Describe the prepared operations so the model lists pending Reminders to resolve conversational references and asks the operator when more than one record could match rather than guessing an ID.
- [ ] Record bounded trace evidence for edit and cancellation proposals, approvals, rejections, committed mutations, and validation failures.
- [ ] Add application-seam tests for body-only, time-only, combined, rejected, expired, missing-ID, and terminal-record edits.
- [ ] Add application-seam tests for approved, rejected, missing-ID, and terminal-record cancellations and prove approved edits and cancellations wake and recalculate the scheduler.
- [ ] Add orchestration-boundary coverage proving ambiguous conversational matches do not result in an edit or cancellation tool call with a guessed ID.
- [ ] Run the focused Reminder, prepared-tool, approval, scheduler, and runtime tests with `uv` and leave the repository green.
