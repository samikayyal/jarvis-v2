# 01 — Save and inspect approved one-time reminders

**What to build:** Let the authorized operator create one exact, approved one-time Reminder and inspect it later. The complete slice must resolve the strict local due time in the configured operator timezone, persist the approved Reminder independently of the working session, and expose bounded pending and historical views without yet sending due reminders.

**Blocked by:** None — can start immediately.

**Status:** complete

- [x] Add the required non-secret operator-timezone and reminder-database configuration, initially using `Asia/Amman`, without depending on the host timezone.
- [x] Reject missing or invalid IANA timezones during normal configuration validation without writing state or contacting external services.
- [x] Create the SQLite reminder store in the personal runtime data area using only the Python standard library and keep it separate from OpenWA state.
- [x] Persist a short, opaque, stable, collision-checked Reminder ID; exact body; UTC due time; display timezone; lifecycle status; timestamps; and the fields needed for later terminal outcomes.
- [x] Expose `create_reminder` as a prepared operation accepting one non-empty body of at most 4,096 characters and one strict local ISO-8601 date-time.
- [x] Resolve the local date-time under the configured timezone, reject malformed, ambiguous, nonexistent, or non-future times, and render the canonical date, time, and timezone for approval.
- [x] Freeze the exact body, resolved time, timezone, and assigned ID in a non-saveable approval proposal; exact `1` persists it once and exact `9` leaves no Reminder behind.
- [x] Revalidate that the due time is still in the future when approval is applied; an expired proposal must perform no mutation.
- [x] Expose `list_reminders` as a read-only prepared operation that shows pending Reminders by default and supports explicitly including retained terminal records.
- [x] Return bounded list entries containing the stable ID, exact body, canonical local due time with timezone, and lifecycle status in deterministic order.
- [x] Ensure starting a new working session does not remove or alter an approved Reminder.
- [x] Record bounded trace evidence for create proposals, approvals, rejections, committed creation, and list operations using the existing sensitive-runtime-data policy.
- [x] Wire the store and prepared operations into the existing prepared-tool collection without adding slash commands, recipients, contacts, queues, services, or Python dependencies.
- [x] Add application-seam tests using real temporary SQLite state and a controllable clock for approval grammar, frozen proposals, validation boundaries, stable IDs, pending listing, retained-record listing, session independence, and store reopen behavior.
- [x] Run the focused Reminder, approval, configuration, and service-composition tests with `uv` and leave the repository green.

## Answer

Implemented in the personal runtime with required IANA timezone and rooted
SQLite configuration, exact approved creation, bounded pending/history listing,
durable reopen behavior, and application-seam coverage. Due delivery remains in
ticket 02.
