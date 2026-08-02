Type: task
Status: resolved
Blocked by: 05

## Question

Verify authenticated API health, outbound and inbound text messaging, session persistence across a controlled service restart, automatic recovery expectations after laptop reboot, LAN-only exposure, and steady-state CPU/RAM usage. Does the active `whatsapp-web.js` deployment meet the destination on this hardware?

## Comments

### Verification progress — 2026-07-31

- Confirmed the exact inbound test message through `GET /api/sessions/:sessionId/messages` without recording sender identifiers.
- Sent an API reply to that same chat; OpenWA recorded the outgoing message as `delivered`.
- Before the controlled container restart began, WhatsApp sent a `LOGOUT` disconnect. OpenWA consequently deleted the session's credentials by design, so the subsequent restart could not restore them and produced a new QR.
- The pre-logout runtime was using OpenWA's default remote, integrity-unchecked pinned WhatsApp Web HTML. Changed the deployment to `WWEBJS_WEB_VERSION=off`, which OpenWA's own warning identifies as the opt-out that uses WhatsApp's first-party served build.
- Recreated OpenWA successfully with the first-party build setting. Health is green, automatic session start is confirmed, and `jarvis` is `qr_ready` for the required re-pair. Persistence verification remains open until this configuration survives a controlled restart.
- After the second pairing, the first-party web build reached `ready` but WhatsApp issued another `LOGOUT` after about three minutes without any service restart. OpenWA again removed the invalidated web credentials. This demonstrates that `whatsapp-web.js` is not currently stable for this account under either OpenWA web-version mode.
- Activated the pre-agreed Baileys fallback as the only engine, with its separate auth root `/app/data/baileys`. OpenWA is healthy, the Baileys plugin is loaded/enabled, automatic start ran, and the same `jarvis` session is `qr_ready` for Baileys pairing.

## Answer

Resolved on 2026-07-31. The active Baileys deployment meets the messaging-layer destination; `whatsapp-web.js` does not meet it for this account.

- After pairing, Baileys reached `ready` and remained there throughout the pre-restart observation window. Sanitized log checks found no `LOGOUT` or failed events.
- A new exact inbound text was persisted as `incoming/text`. The authenticated API matched it using the session's internal ID while keeping the chat ID inside the remote shell, then accepted an outbound reply. OpenWA persisted that reply as `outgoing/sent`, and the user confirmed it arrived on the sending phone.
- The API route nuance is material: `/api/sessions/:sessionId/messages` expects the session's internal ID, not the human-readable `jarvis` name. Querying with the name returns an empty collection even when messages are present.
- Added `stop_grace_period: 45s` to `openwa-api`; `docker compose config --quiet` passed. A controlled `docker compose up -d --force-recreate openwa-api` then recreated the service. Docker reports the 45-second timeout, container health returned to `healthy`, and `jarvis` returned directly to `ready` with retained Baileys auth state and no new QR.
- Two additional post-restart samples remained `healthy`/`ready`. Docker's `unless-stopped` policy, Compose automatic session start, and the successful controlled restoration establish the expected service/session recovery path after a host reboot; a disruptive laptop reboot was not required for this verification.
- The service remains bound only to the exact private LAN address and UFW remains active with TCP 2785 allowed only from the home `/24`.
- Settled post-restart OpenWA usage was 132.0-132.6 MiB (3.48-3.50% of host memory) at 0.01% CPU across three samples. The host had 2.6 GiB memory available and 2.5 GiB of its 4.0 GiB swap free.

The two web-engine attempts both received WhatsApp `LOGOUT` without a service restart and lost their invalidated credentials by design. Baileys is therefore the sole active engine and the verified production choice for this messaging-layer setup.
