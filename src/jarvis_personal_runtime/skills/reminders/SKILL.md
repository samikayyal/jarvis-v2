---
name: reminders
description: Create, list, edit, or cancel one-time reminders sent to the authorized operator.
---

Resolve relative dates from the current time and configured operator timezone. Obtain missing time context or clarify a materially ambiguous due time. Use an exact future local due time in the format accepted by the tool. Reminders store text for one-time delivery to the operator; they do not execute future tasks, recur, or address third parties.

Use `create_reminder` with the intended reminder text and due time. Use `list_reminders` to identify existing reminders. Select the pending-only view unless retained history is needed. Use returned stable IDs, and clarify if several pending reminders match.

Use `edit_reminder` for changes to a pending reminder, passing null for unchanged fields. Use `cancel_reminder` for immediate cancellation of the identified pending reminder. These operations do not use the terminal/Google approval flow.

Confirm the saved ID, text, date, time, and timezone from the result. Distinguish saved, cancelled, sent, failed, and unknown outcomes; successfully saving a reminder does not mean it was delivered.
