# 02 — Send each due reminder once

**What to build:** Make each approved Reminder send its exact stored body to the authorized operator when its Due time arrives. The complete slice must schedule inside the personal assistant process, use OpenWA only for the immediate transport attempt, record a terminal outcome, and never retry automatically.

**Blocked by:** 01 — Save and inspect approved one-time reminders.

**Status:** ready-for-agent

- [ ] Add one asynchronous scheduler to the existing personal assistant service process; do not add cron, Celery, Redis, a queue, another worker process, or another deployed service.
- [ ] Have the scheduler select the earliest pending Reminder, wait until its Due time without periodic polling, and wake early when an approved creation changes the next Due time.
- [ ] At the Due time, read the frozen body and make no model request, reinterpretation, expansion, summarization, or chunking decision.
- [ ] Submit exactly one text-send call directly through the existing OpenWA sender using the configured operator chat ID; expose no recipient argument to the model.
- [ ] Prevent one Reminder from being selected for more than one transport call during the running process without introducing a general outbox or retry system.
- [ ] Record `sent` and the accepted outbound message ID when OpenWA definitely accepts the send.
- [ ] Record `failed` when OpenWA definitely rejects or fails the send and make that result terminal.
- [ ] Record `unknown` when the transport outcome may have been accepted and make that result terminal.
- [ ] Never retry a `sent`, `failed`, or `unknown` Reminder automatically.
- [ ] Retain terminal Reminder records and include them only when historical records are explicitly requested through listing.
- [ ] Allow a due Reminder to send independently while an ordinary request, approval, terminal operation, or ordinary reply is active; add no ordering guarantee for simultaneous outbound messages.
- [ ] Record bounded trace evidence for the delivery attempt and terminal outcome, including the outbound message ID when one exists.
- [ ] Start the scheduler with the service lifecycle and cancel its wait during ordinary shutdown without adding late-delivery or downtime-recovery behavior.
- [ ] Add application-seam tests with real temporary SQLite state, a controllable clock, and a fake OpenWA sender for next-due selection, wake-on-create, chronological delivery, exact body and recipient, one-call behavior, foreground-request independence, and terminal-row exclusion.
- [ ] Test definite acceptance, definite failure, and ambiguous failure, proving the correct retained outcome and no later automatic retry in every case.
- [ ] Add the smallest existing-style composition test proving the scheduler uses the reminder store and operator-only OpenWA sender constructed by the service.
- [ ] Run the focused Reminder scheduler, OpenWA sender, message-flow, and service-composition tests with `uv` and leave the repository green.
