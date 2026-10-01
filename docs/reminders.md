# Reminder lifecycle and supervised acceptance

## Capability and ownership

A Reminder is Jarvis's narrow proactive exception: one authorized instruction to
send exact stored text once to the authorized operator at one future instant.
It is not a scheduled message to a third party and is not a general automation
or background-job facility.

Jarvis owns the durable schedule, lifecycle decisions, and one in-process
scheduler. Reminder records live in the configured SQLite database beneath the
personal runtime root, independently of the memory-only working session and of
OpenWA's databases. OpenWA owns WhatsApp transport, pairing, named-session
readiness, and message persistence. Jarvis calls OpenWA only when a Reminder is
due, using the configured operator chat ID; OpenWA does not own Reminder time or
state.

## Configuration and validation

The non-secret `[runtime]` configuration requires both settings shown in
`deployment/personal-runtime/jarvis.toml.example`:

```toml
operator_timezone = "Asia/Amman"
reminder_database_path = "data/reminders.sqlite3"
```

`operator_timezone` must be an IANA timezone and does not inherit the host
timezone. `reminder_database_path` must resolve beneath the runtime root. The
normal `jarvis-personal-runtime --check` command validates both settings without
creating the SQLite database, binding a listener, or contacting OpenWA, OpenAI,
Google, or a configured MCP service. Normal service startup creates or opens the
database.

## Lifecycle and operating contract

`create_reminder`, `edit_reminder`, and `cancel_reminder` commit during the
authorized request, without a follow-up `1` or `9` confirmation. The tool
result for creation and editing reports the stable ID, exact body, and local Due
time. Jarvis can also list the stored Reminder. Editing and cancellation require
a stable ID; Jarvis lists pending records and asks which one the operator means when a
conversational reference is ambiguous.

Bodies must contain 1 through 4,096 characters, and Due times must be strict,
unambiguous future local date-times. Only pending Reminders can be edited or
cancelled.

`list_reminders` is read-only and returns pending records by default. An
explicit historical listing also includes retained terminal records:

- `sent`: OpenWA definitely accepted the one outbound attempt; the accepted
  outbound message ID is retained.
- `failed`: the one attempt definitely failed.
- `unknown`: the attempt may have been accepted, so Jarvis does not retry it.
- `cancelled`: the operator cancelled it before an attempt began.

At the Due time, the scheduler claims the pending record for its sole attempt
and sends its exact stored body as one OpenWA text message to the configured
operator chat ID. It does not call the model, look up a contact, select a
recipient, split or rewrite the body, or retry any outcome. Terminal and
in-flight records are excluded from future delivery selection. The send can run
while a foreground request is active; there is no ordering promise when an
ordinary reply and a Reminder are simultaneous.

The sensitive rotating runtime trace provides bounded events for committed
mutations, delivery attempts, accepted outbound IDs, definite failures, and
ambiguous outcomes. Protect it as verbatim runtime data: do not copy bodies,
phone or chat identifiers, credentials, or raw payloads
into ordinary logs or tickets.

This capability deliberately has no recurrence, snoozing, contact lookup,
Google Calendar dependency, external scheduler, queue, new resident service, or
additional Python package. It also makes no guarantee about downtime recovery,
late delivery, missed-reminder handling, edit-versus-send races, or ordering
with simultaneous ordinary replies. Those remain explicit exclusions rather
than implicit recovery behavior.

## Human-owned phone acceptance procedure

This procedure verifies an already reviewed and separately activated runtime.
It does not authorize deployment, production activation, configuration changes,
OpenWA recreation, re-pairing, firewall changes, or a new QR scan. Stop if the
service is not healthy, the configured named OpenWA session is not `ready`, an
identity differs from the reviewed configuration, or WhatsApp reports `LOGOUT`.

The human operator owns every WhatsApp request and the final phone-receipt
judgment. Agent-written instructions and server-side records are not evidence of
physical receipt.

1. On the authorized operator's phone, choose a unique short Reminder body and
   a Due time several minutes in the future in the configured operator timezone.
   Record the expected body and canonical local date, time, and timezone without
   recording the phone number or chat ID.
2. From that authorized WhatsApp account, ask Jarvis to create the one-time
   Reminder. Do not ask it to message a named third party; any named person may
   appear only as context inside the body sent back to the operator.
3. Ask Jarvis to list pending Reminders and verify that the created record
   appears once with the same ID, body, and Due time.
4. Optionally, while enough future time remains, ask Jarvis to edit that ID's
   body, Due time, or both. List pending Reminders again and verify the complete
   resulting record, including unchanged fields. Use the resulting body as the
   receipt fixture.
5. Keep the authorized phone available through the Due time. The human confirms
   that one WhatsApp message arrives, that its body exactly matches the final
   stored body, and that no second copy arrives during a reasonable observation
   window. Do not manufacture a second attempt if delivery is delayed or unclear.
6. Ask for Reminder history and verify the record is terminal. A definite
   acceptance should appear as `sent`; preserve `failed` or `unknown` exactly if
   that is the observed outcome and do not retry it automatically.
7. On the host, inspect only bounded trace metadata needed to correlate the
   committed mutation, single delivery attempt, and terminal outcome.
   Record the human's phone confirmation separately from transport acceptance,
   and state explicitly whether this procedure was actually executed.
