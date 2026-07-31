# OpenWA operations and recovery runbook

Run deployment commands on the gateway host. Do not paste secrets, QR data,
phone numbers, chat IDs, or raw authentication material into a shell command
that will be recorded.

## Routine status check

1. Check the service:

   ```bash
   sudo docker compose -f /opt/openwa/compose.yaml ps
   ```

2. Require the `openwa-api` container to report `healthy`.
3. Query the authenticated sessions endpoint from a root shell that reads only
   the required API key from `/opt/openwa/.env` without echoing it.
4. Require the named session to report `ready`.

Treat `healthy` plus any session state other than `ready` as messaging not ready.
`qr_ready` means pairing is required; `failed`, `LOGOUT`, or a persistent
disconnect requires investigation.

## Authenticated message verification

The message-history route uses the session's internal database/API ID in
`:sessionId`, not its human-readable name. Resolve the internal ID from the
authenticated sessions response and retain it only inside the remote shell.

For an end-to-end text check:

1. Ask the sender to transmit a new unique phrase. A numeric prefix avoids phone
   auto-capitalization breaking an exact-body match.
2. Query `GET /api/sessions/:sessionId/messages` and match only the exact body.
3. Retain the matched chat ID in a shell variable; do not print it.
4. Send one reply through `POST /api/sessions/:sessionId/messages/send-text`.
5. Record only direction and status, and ask the user to confirm receipt.

Querying the history route with the human-readable session name can return an
empty collection even while matching rows exist. Do not interpret that result
as an ingestion failure until the internal ID has been used.

## Controlled service recreation

Use a recreation when applying Compose changes or proving persistence:

```bash
sudo docker compose -f /opt/openwa/compose.yaml config --quiet
sudo docker compose -f /opt/openwa/compose.yaml up -d --force-recreate openwa-api
```

Then wait for both container health `healthy` and session status `ready`. A
fresh QR after an otherwise controlled recreation means pairing persistence
failed. Confirm Docker still reports a 45-second stop timeout.

Do not repeatedly recreate or re-pair after a WhatsApp `LOGOUT`; repeated linked
device attempts may be throttled.

## Expected host-boot recovery

Docker is enabled at boot, the container uses `restart: unless-stopped`, and
OpenWA automatically starts retained sessions. The successful controlled
recreation demonstrated that Baileys returns to `ready` from persistent state
without a QR. A full disruptive laptop reboot has not yet been performed as a
separate acceptance test.

After any host reboot, verify both health layers rather than assuming the
restart policy restored WhatsApp connectivity.

## Manual engine switch

Only one engine may be active for this account.

1. Confirm current container and session state.
2. Preserve both auth roots in `openwa-data`; do not delete or export them.
3. Change only `ENGINE_TYPE` in the `openwa-api` Compose environment to the
   target value: `baileys` or `whatsapp-web.js`.
4. Keep both engine path settings, automatic session startup, and the 45-second
   stop grace unchanged.
5. Validate and recreate the service using the controlled-recreation commands.
6. Require `healthy` and `ready`, then repeat inbound/outbound and persistence
   verification.

Baileys currently has valid retained authentication and should restore without
pairing. Both previous web credentials were invalidated and deleted after
WhatsApp `LOGOUT`, so switching to `whatsapp-web.js` currently requires a new QR
and is not recommended without a reason to reassess the decision.

## Failure handling

### `LOGOUT`

Stop. Preserve current state and capture only sanitized chronology and reason
codes. Do not delete credentials or repeatedly re-pair. OpenWA intentionally
deletes engine credentials that WhatsApp has invalidated, so a new QR after
`LOGOUT` is expected and is not a Docker-volume failure.

### Ordinary disconnect

Distinguish a transient network reconnect from `LOGOUT`. Monitor session state
for recovery and count sanitized disconnect/failure events without printing raw
logs that may contain message or account data. Escalate only if the disconnect
persists or the session becomes `failed`.

### Healthy container, unavailable messaging

Check the authenticated session state. The HTTP readiness endpoint does not
cover WhatsApp connectivity.

### Empty message history after a known inbound message

First confirm that the internal session ID—not the human-readable name—was used
in the route. If the API still returns no result, compare sanitized row counts
between the normalized `messages` table and Baileys' raw store before forming a
new upstream-defect hypothesis.

## Backup and upgrade discipline

Back up the complete `openwa-data` volume as one unit while the service is
stopped or in a database-consistent state. Protect the backup as sensitive: it
contains API-key state, message data, media, and WhatsApp pairing credentials.
Do not place backups in this repository.

Before an upgrade:

1. record the intended release and immutable image digest;
2. review upstream migrations and engine changes;
3. take a protected volume backup;
4. change the pinned image deliberately;
5. validate Compose and recreate the service;
6. repeat health, session, inbound/outbound, restart-persistence, exposure, and
   resource checks.
