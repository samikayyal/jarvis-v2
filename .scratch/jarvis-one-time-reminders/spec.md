Status: ready-for-agent

## Problem Statement

The authorized operator cannot ask Jarvis to remember a one-time future obligation and receive the exact reminder over WhatsApp at the intended time. The current personal assistant runtime is reactive: it can answer an admitted request, but it cannot retain an approved reminder independently of the working session, expose that reminder for later editing or cancellation, or initiate an outbound message when a due time arrives.

The operator needs a deliberately small reminder capability. A request such as “remind me to message Sara tomorrow at noon” must produce one exact reminder body, resolve “tomorrow at noon” in the configured operator timezone, obtain approval for the exact body and time, and send that stored body back to the operator at the due time. This is not authority to contact Sara or any other third party.

## Solution

Jarvis will support approved, one-time reminders addressed only to the authorized operator. The model may draft the reminder body and interpret natural-language time during the foreground request, but it must pass exact structured values to prepared reminder operations. Jarvis will preview every create, edit, and cancellation and will mutate reminder state only after the operator replies with the existing exact approval choice.

Approved reminders will be stored in a small SQLite database. One asynchronous scheduler inside the existing personal assistant process will wait for the next due reminder and wake early whenever a reminder is created, edited, or cancelled. At the due time it will submit the exact stored body once through the existing operator-only OpenWA sender. OpenWA remains an immediate messaging transport and does not own reminder time or state.

The capability will expose four narrow prepared operations: create, edit, list, and cancel. It will add no external scheduler, queue, resident service, or Python dependency.

## User Stories

1. As the authorized operator, I want to say “remind me to message Sara tomorrow at noon,” so that I can remember the obligation without constructing a formal timestamp.
2. As the authorized operator, I want Jarvis to draft a concise reminder body from my request, so that the eventual reminder contains enough context to act.
3. As the authorized operator, I want the reminder to be sent to me, so that asking to message another person does not authorize Jarvis to contact that person.
4. As the authorized operator, I want the exact proposed reminder body shown before it is stored, so that I can catch incorrect wording or intent.
5. As the authorized operator, I want the exact calendar date, local time, and timezone shown before a reminder is stored, so that relative phrases cannot silently resolve to the wrong instant.
6. As the authorized operator, I want creation to require the exact existing approval response, so that conversational acknowledgements cannot accidentally schedule reminders.
7. As the authorized operator, I want rejection to leave no reminder behind, so that declining a proposal has no hidden side effect.
8. As the authorized operator, I want a created reminder to receive a short stable ID, so that the exact record can be referenced later.
9. As the authorized operator, I want to list pending reminders without approval, so that I can see my future obligations quickly.
10. As the authorized operator, I want pending reminders to be the default list view, so that completed history does not obscure actionable reminders.
11. As the authorized operator, I want to ask for completed reminders when needed, so that I can inspect whether an earlier reminder was sent, failed, became uncertain, or was cancelled.
12. As the authorized operator, I want to refer conversationally to a clearly matching reminder, so that ordinary use does not require memorizing its ID.
13. As the authorized operator, I want Jarvis to ask which reminder I mean when multiple pending reminders match, so that it never guesses which record to mutate.
14. As the authorized operator, I want to change only a reminder’s body, so that I can correct or improve its wording without rescheduling it.
15. As the authorized operator, I want to change only a reminder’s due time, so that I can reschedule it without recreating its text.
16. As the authorized operator, I want to change both body and due time in one edit, so that one approval can cover the complete resulting reminder.
17. As the authorized operator, I want an edit preview to show the complete resulting body and time, so that unchanged fields are not hidden during approval.
18. As the authorized operator, I want a rejected edit to preserve the existing reminder exactly, so that previewing a change is safe.
19. As the authorized operator, I want an approved edit to preserve the reminder’s stable ID, so that it remains the same reminder.
20. As the authorized operator, I want to cancel a pending reminder, so that an obsolete reminder will not be sent.
21. As the authorized operator, I want cancellation to show the exact reminder and require approval, so that a mistaken reference cannot silently remove it.
22. As the authorized operator, I want a rejected cancellation to leave the reminder pending, so that rejection is non-destructive.
23. As the authorized operator, I want edits and cancellations of non-pending reminders rejected, so that historical outcomes cannot be rewritten as future work.
24. As the authorized operator, I want times interpreted in `Asia/Amman`, so that Jarvis does not depend on the server’s local timezone.
25. As the authorized operator, I want ambiguous or invalid local times rejected, so that Jarvis never invents an instant.
26. As the authorized operator, I want a due time that is no longer in the future at approval to be rejected, so that approval cannot create an already-expired reminder.
27. As the authorized operator, I want the exact approved body stored, so that later model behavior cannot change the reminder.
28. As the authorized operator, I want reminder delivery to require no model call, so that model availability or nondeterminism at the due time cannot alter the message.
29. As the authorized operator, I want the exact stored body sent once when due, so that the received reminder matches what I approved.
30. As the authorized operator, I want one reminder to map to one OpenWA text message, so that a single reminder cannot produce a series of chunks.
31. As the authorized operator, I want bodies longer than OpenWA’s 4,096-character limit rejected, so that the delivery contract remains one bounded send.
32. As the authorized operator, I want a reminder to be sent even while Jarvis is processing another request, so that unrelated work does not postpone its due time.
33. As the authorized operator, I want a definite OpenWA acceptance recorded as sent with its outbound message ID, so that successful transport acceptance has evidence.
34. As the authorized operator, I want a definite send failure recorded as failed without an automatic retry, so that each reminder has only one attempt.
35. As the authorized operator, I want an ambiguous OpenWA result recorded as unknown without an automatic retry, so that uncertainty cannot produce a duplicate reminder.
36. As the authorized operator, I want cancelled and completed reminder records retained, so that outcomes remain inspectable without adding a separate history system.
37. As the authorized operator, I want reminder creation, editing, cancellation, attempts, and outcomes represented in the runtime trace, so that behavior can be diagnosed using the existing evidence channel.
38. As the authorized operator, I want reminders to remain available after starting a new working session, so that `/new` does not erase approved future obligations.
39. As the operator maintaining Jarvis, I want reminder state stored independently of OpenWA’s database, so that assistant decisions do not leak into messaging-gateway ownership.
40. As the operator maintaining Jarvis, I want OpenWA invoked only when a reminder becomes due, so that the gateway remains an immediate transport boundary.
41. As the operator maintaining Jarvis, I want one in-process scheduler rather than another deployed service, so that the feature adds minimal operational burden.
42. As the operator maintaining Jarvis, I want reminder state in SQLite using the Python standard library, so that the feature adds no package or infrastructure dependency.
43. As the operator maintaining Jarvis, I want creating, editing, or cancelling a reminder to wake the scheduler, so that it always waits for the current earliest due reminder.
44. As the operator maintaining Jarvis, I want idle scheduling to consume no polling loop, so that waiting for future reminders has negligible runtime cost.
45. As the operator maintaining Jarvis, I want stored terminal outcomes never selected for delivery, so that listing history cannot cause replay.

## Implementation Decisions

- A Reminder is a one-time instruction to send exact stored text to the authorized operator. It is not a scheduled third-party message, recurring task, calendar event, or general background job.
- Reminder delivery always uses the configured operator chat ID. No recipient argument is exposed to the model, and the feature introduces no contact lookup, arbitrary JID, or phone-number handling.
- The prepared operation surface consists of `create_reminder`, `edit_reminder`, `list_reminders`, and `cancel_reminder`. These operations join the existing prepared-tool collection and do not add deterministic slash commands.
- `create_reminder` accepts a non-empty body and one strict local ISO-8601 date-time. The reminder capability resolves that wall time with the configured IANA operator timezone and stores the corresponding UTC instant.
- `edit_reminder` accepts a stable reminder ID and at least one of a replacement body or replacement local due time. It validates the complete resulting reminder before proposing the edit and preserves the ID after approval.
- `list_reminders` is read-only and shows pending reminders by default. It supports an explicit request to include retained terminal records and returns bounded records containing the stable ID, exact body, local due time with timezone, and status.
- `cancel_reminder` accepts a stable reminder ID and applies only to a pending reminder.
- Reminder IDs are short, opaque, stable, and collision-checked. The model may call the list operation to resolve a conversational reference, but the model must ask the operator when the reference does not identify exactly one pending reminder.
- Create, edit, and cancel are prepared writes. Each returns an approval proposal containing the complete exact reminder body, local date, local time, timezone, and stable ID when one already exists. They use the existing non-saveable approval grammar: exact `1` approves once and exact `9` rejects.
- A write proposal freezes all values in its continuation. Approval persists those frozen values; it never reparses the original natural-language request or accepts altered arguments. Rejection performs no database mutation.
- Creation assigns the stable ID as part of the frozen proposal, so the approval preview and persisted reminder identify the same record.
- The configured operator timezone is a required non-secret setting and initially uses `Asia/Amman`. Runtime validation rejects an invalid IANA name. The host operating-system timezone is irrelevant.
- Natural-language date interpretation remains model work during the foreground request. The application boundary accepts only the strict local date-time, resolves it deterministically under the configured operator timezone, rejects ambiguous or nonexistent local times, converts it to UTC, and renders the canonical local value for approval.
- The due time must still be strictly in the future when approval is applied. A proposal that has become due while waiting for approval is rejected without mutation and must be proposed again with a future time.
- Reminder bodies must be non-empty and at most 4,096 characters. The length is validated before approval and again at the state boundary. Bodies are never split, summarized, expanded, regenerated, or passed back through the model at delivery time.
- One SQLite database in the personal runtime’s data area owns reminder state. It is separate from OpenWA’s database and uses the Python standard library; schema creation and access do not add a migration framework or external persistence service.
- A reminder record contains the stable ID, exact body, UTC due time, configured timezone used for display, creation and update timestamps, lifecycle status, attempt timestamp when applicable, completion timestamp when applicable, accepted outbound message ID when available, and a bounded failure classification when applicable.
- Externally meaningful lifecycle statuses are `pending`, `sent`, `failed`, `unknown`, and `cancelled`. An implementation may use one transient in-flight marker to ensure a pending row is selected for no more than one call, but it must not create automatic retry or recovery behavior.
- Terminal rows are retained in SQLite and are never selected for delivery. No cleanup or retention process is introduced in this version.
- One asynchronous scheduler runs inside the existing personal assistant service process. It reads the earliest pending due time, waits without periodic polling, and can be woken when an approved create, edit, or cancellation changes the next due reminder.
- When a reminder becomes due, the scheduler reads its frozen body, records that its only attempt has begun, and calls the existing OpenWA text sender directly with the configured operator chat ID. It does not route the reminder through inbound webhook handling or the normal inbound-response flow.
- A reminder send is independent of the single active foreground request. It may occur while a model-and-tool loop, approval, terminal operation, or ordinary outbound reply is active. No additional ordering policy is introduced for simultaneous ordinary replies and reminders.
- A successful OpenWA response records `sent` and the accepted outbound message ID. A definite failure records `failed`. A timeout, transport error, or response ambiguity that may have been accepted records `unknown`. Every outcome is terminal, and no reminder receives an automatic second attempt.
- Reminder operations and the scheduler use the existing runtime trace for bounded events covering proposal, approval or rejection, committed mutation, delivery attempt, and terminal outcome. The exact reminder body is already sensitive runtime data and follows the existing trace and runtime-data protections.
- The service composition root constructs the reminder store, reminder prepared operations, scheduler, and existing OpenWA sender as one capability. Service startup starts the scheduler, and ordinary service shutdown cancels its wait without inventing reminder recovery semantics.
- The feature implements the ownership decision in the accepted reminder-scheduling ADR: Jarvis owns durable schedule and decision state; OpenWA remains immediate transport, pairing, readiness, and message persistence.
- The implementation should be the smallest coherent addition to the active personal runtime. It must not restore or port the historical control-plane, outbox, broker, custom worker, or recovery architecture.

## Testing Decisions

- Tests assert externally observable reminder behavior rather than SQLite statements, table layout, task primitives, private helpers, or exact sleep calls.
- The primary test seam combines the real prepared reminder operations, real temporary SQLite state, real scheduling behavior, a controllable clock, and a fake OpenWA sender. This is the highest seam that is deterministic without involving a live model or gateway.
- At this seam, creation tests prove canonical preview content, exact `1` approval, exact `9` rejection, frozen proposal values, no mutation before approval, stable ID assignment, configured-timezone rendering, UTC storage behavior as observed through listing, and rejection of invalid bodies or times.
- Editing tests prove body-only, time-only, and combined edits; full-result previews; ID preservation; no mutation before approval; rejection preservation; ambiguous-reference handling at the orchestration boundary; missing IDs; and rejection of terminal records.
- Listing tests prove pending-only defaults, explicit inclusion of retained terminal records, stable IDs, exact stored bodies, canonical local time display, deterministic ordering, and bounded output.
- Cancellation tests prove full reminder previews, exact approval grammar, no mutation on rejection, terminal cancellation on approval, removal from pending delivery, and rejection of missing or non-pending IDs.
- Scheduler tests prove that the earliest pending reminder is selected, no polling is required, approved create/edit/cancel operations wake and recalculate the wait, multiple reminders are delivered in due-time order, and terminal rows are never selected.
- Delivery tests prove the exact approved body and configured operator chat ID reach the fake OpenWA sender once, without a model call or message chunking.
- Outcome tests make the fake sender return acceptance, definite failure, and ambiguous failure. They assert `sent`, `failed`, and `unknown` respectively, accepted outbound-ID retention, one call only, and no later automatic retry.
- Concurrency coverage proves a due reminder can call the fake sender while the personal runtime has an active foreground request. It does not specify ordering when an ordinary reply and reminder occur simultaneously.
- Persistence coverage closes and reopens the SQLite store and proves pending and terminal records remain queryable. It does not define whether an overdue pending reminder is delivered after downtime.
- Validation coverage includes empty and 4,097-character bodies, exactly 4,096 characters, invalid IANA zones, malformed local date-times, past times, times that become past before approval, and ambiguous or nonexistent local wall times where the configured zone permits them.
- One existing-style service composition test proves the reminder capability is constructed with the configured timezone and database location, included in the prepared-tool collection alongside other configured tools, connected to the existing operator-only OpenWA sender, and started with the service lifecycle.
- Existing approval tests are prior art for exact `1` and `9` handling and non-saveable prepared writes. Existing Google write tests are prior art for freezing a validated proposal before approval. Existing OpenWA message-flow tests remain authoritative for immediate send acceptance, definite versus ambiguous failures, operator-recipient enforcement, and the 4,096-character transport limit.
- Tests do not exercise natural-language interpretation because that behavior belongs to the model. They begin with the exact structured arguments presented to the prepared reminder operations.
- A final supervised acceptance check schedules a short future reminder from the authorized WhatsApp account, verifies the exact approval preview, optionally edits its body or time, and confirms that the approved body is received once on the operator’s phone. This is human-observed delivery evidence, not an automated live-gateway test.

## Out of Scope

- Sending a scheduled message to Sara or any other third party.
- Multiple recipients, groups, contact lookup, arbitrary phone numbers, or model-selected chat IDs.
- Recurring reminders, recurrence rul es, snoozing, relative-duration timers, reminder chains, or dependencies between reminders.
- Automatic retry after definite failure, ambiguous failure, timeout, or any other attempted delivery.
- Guaranteed exactly-once WhatsApp delivery; OpenWA exposes no client idempotency key or transactional send-and-state operation.
- Downtime recovery semantics, late delivery, missed-reminder notifications, catch-up windows, or a guarantee for reminders that become due while Jarvis is not running.
- Special behavior for the rare race between an edit or cancellation and a delivery already beginning.
- Ordering guarantees between a reminder and an unrelated ordinary reply sent at the same instant.
- Rich media, attachments, message templates, mentions, multiple message chunks, or bodies longer than 4,096 characters.
- Google Calendar integration or using calendar events as the reminder store or scheduler.
- OpenWA changes, OpenWA plugins, delayed OpenWA jobs, or storing reminder state in OpenWA’s database.
- Celery, Redis, cron, systemd timers per reminder, external queues, a separate worker process, or any other resident infrastructure tier.
- New slash commands, a graphical management interface, public reminder APIs, or multi-operator support.
- Terminal-action scheduling, scheduled model calls, proactive monitoring, or any general-purpose automation framework.
- Cleanup, archival, or retention policies for terminal reminder records.
- A general historical outbox or restoration of the retired control-plane architecture.

## Further Notes

- The pinned OpenWA version explicitly has no scheduled or delayed outbound sending. Its role in this feature begins only when the Jarvis scheduler submits an already-due body.
- This spec intentionally relaxes the current reactive-only product scope for this one bounded Reminder concept. It does not authorize other scheduled work, monitoring, or unsolicited assistant behavior.
- Starting a new working session does not affect approved reminders because reminder state is not working-session state.
- The always-running service assumption is deliberate. SQLite provides simple durable state and supports later edits and inspection, but this version does not turn persistence into a promise about overdue execution after downtime.
- Implementation and verification commands for this Python project must use `uv`.
