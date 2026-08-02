Type: task
Status: resolved
Blocked by: 01, 06

## Question

Prepare and document a reversible manual switch from `whatsapp-web.js` to Baileys at the pinned OpenWA revision, including whether re-pairing is required, separate auth-state handling, validation steps, and rollback. Do not leave both engines active simultaneously.

## Comments

## Answer

Resolved on 2026-07-31. Baileys is now the active engine rather than merely a prepared fallback. The switch remains reversible, but only one engine may be active at a time.

### Engine-switch procedure

1. Confirm the current session state and container health. Preserve both auth roots in the persistent `openwa-data` volume: `/app/data/sessions` for `whatsapp-web.js` and `/app/data/baileys` for Baileys. Do not copy their contents out or delete either root during a switch.
2. Change only `ENGINE_TYPE` in the `openwa-api` Compose environment to the target engine. Keep `BAILEYS_AUTH_DIR=/app/data/baileys`, `SESSION_DATA_PATH=/app/data/sessions`, `AUTO_START_SESSIONS=true`, and `stop_grace_period: 45s` in place. Never run both engines simultaneously for this account.
3. Run `docker compose -f /opt/openwa/compose.yaml config --quiet`, then apply the change with `docker compose -f /opt/openwa/compose.yaml up -d --force-recreate openwa-api`.
4. Wait independently for container health `healthy` and the `jarvis` session status `ready`. HTTP health alone is insufficient. A first activation with no valid target-engine auth requires QR pairing; retained valid auth should restore without a QR.
5. Verify a unique inbound text through the authenticated messages API using the session's internal ID, retain the matching chat ID only inside the remote shell, send one reply through `POST .../messages/send-text`, and confirm receipt. Then perform a controlled recreation and require `ready` without a new QR.

### Current rollback position

To retry `whatsapp-web.js`, set `ENGINE_TYPE: whatsapp-web.js`, validate Compose, and recreate the single service using the same steps. Both prior web credentials were invalidated and deleted after WhatsApp issued `LOGOUT`, so rollback currently requires a fresh web QR pairing. Because this happened twice under both the remotely pinned and first-party web builds, do not retry repeatedly or delete credentials; stop and reassess if WhatsApp invalidates another pairing. Switching back to Baileys reuses `/app/data/baileys` as long as that retained auth remains valid.
