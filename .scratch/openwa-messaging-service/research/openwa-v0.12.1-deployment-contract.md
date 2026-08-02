# OpenWA v0.12.1 minimal messaging deployment contract

Research date: 2026-07-31

Primary-source snapshot: OpenWA release `v0.12.1`, commit [`31c5499a9beea1c5b460a4854ed68587b25f53d2`](https://github.com/rmyndharis/OpenWA/tree/31c5499a9beea1c5b460a4854ed68587b25f53d2). This is the latest tagged release at the time of research. The relevant deployment/auth/engine files are unchanged on `main` commit `6d1bfedaa03223742d2c0c5cad8fa5b1f521e169`; the recommendation is still to pin the tested release, not moving `main` or `latest`.

## Decision

Pin the source checkout to `v0.12.1` (and record/verify commit `31c5499...`), then use the shipped production `docker-compose.yml`. Run its default profile only, with `whatsapp-web.js`, SQLite, local storage, one persistent `openwa-data` volume, and no PostgreSQL/Redis/MinIO profiles. Do not auto-pull or auto-upgrade.

The shipped default stack starts two core services:

1. `openwa-api`, which builds from the pinned checkout and runs the API plus bundled dashboard.
2. `docker-proxy`, a pinned `tecnativa/docker-socket-proxy:v0.4.2` sidecar used by OpenWA's optional built-in datastore orchestration.

PostgreSQL, Redis, and MinIO are profile-gated and are not part of the default minimal run. The proxy can be explicitly disabled with an override if built-in datastore orchestration will never be used; OpenWA degrades gracefully without it. For the first deployment, retaining the shipped two-service default minimizes divergence from upstream. [Compose core services and optional-proxy semantics](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/docker-compose.yml#L4-L50) [Profile-gated services](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/docker-compose.yml#L238-L300) [Production commands and profiles](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/README.md#L226-L250)

## Configuration contract

OpenWA's precedence is: real process/container environment, then project `.env`, then persistent `data/.env.generated`. The app creates `data/.env.generated` on first boot with SQLite, local storage, Redis/queue off, and no optional Docker profiles. Dashboard configuration is written back to that generated file and requires a restart to apply. [Environment precedence and generated defaults](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/config/load-env.ts#L7-L16) [First-boot generated configuration](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/config/load-env.ts#L63-L94) [Dashboard save and restart contract](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/infra/infra.controller.ts#L949-L980)

For this single-session deployment, explicitly pin these operational choices (rather than relying only on generated defaults):

```dotenv
NODE_ENV=production
DATABASE_TYPE=sqlite
DATABASE_NAME=./data/openwa.sqlite
DATABASE_SYNCHRONIZE=false
STORAGE_TYPE=local
STORAGE_LOCAL_PATH=./data/media
REDIS_ENABLED=false
QUEUE_ENABLED=false
CACHE_ENABLED=false
ENGINE_TYPE=whatsapp-web.js
SESSION_DATA_PATH=./data/sessions
PUPPETEER_HEADLESS=true
PUPPETEER_ARGS=--no-sandbox,--disable-setuid-sandbox,--disable-dev-shm-usage,--disable-gpu
AUTO_START_SESSIONS=true
API_MASTER_KEY=<one newly generated high-entropy secret>
API_KEY_PEPPER=<a different newly generated high-entropy secret>
CSP_UPGRADE_INSECURE_REQUESTS=false
```

`AUTO_START_SESSIONS=true` is for restoring an already-authenticated session after boot; pairing still happens interactively once. `API_KEY_PEPPER` should be set before keys are created because changing it later invalidates existing key hashes. The shipped Compose forwards most of the list, including `ENGINE_TYPE`, session/Puppeteer values, `AUTO_START_SESSIONS`, `API_MASTER_KEY`, and the plain-HTTP CSP switch. It does **not** forward `API_KEY_PEPPER`, so a small Compose override must add that variable to `openwa-api.environment` (or it must be placed in the persistent `data/.env.generated` inside `/app/data`). [Compose environment forwarding](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/docker-compose.yml#L99-L204) [Engine defaults and separate auth roots](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/config/configuration.ts#L154-L178) [Minimal single-session settings](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/.env.minimal#L7-L45) [Production pepper warning](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/main.ts#L79-L85)

There are always two SQLite files in the data volume: `./data/main.sqlite` for API keys/audit data and `./data/openwa.sqlite` for application data. They must remain distinct. [Database defaults](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/config/configuration.ts#L104-L128) [Collision guard](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/config/env.validation.ts#L12-L37)

## Persistence contract

The single named volume `openwa-data:/app/data` is authoritative. It contains both SQLite databases, the generated environment file, bootstrap API-key file, local media, plugins, whatsapp-web.js auth state, and Baileys auth state. Losing it loses API keys and WhatsApp pairings. The relevant default in-container paths are:

- `/app/data/main.sqlite`
- `/app/data/openwa.sqlite`
- `/app/data/.env.generated`
- `/app/data/.api-key`
- `/app/data/media`
- `/app/data/sessions/session-<session-name>` for whatsapp-web.js
- `/app/data/baileys/<session-name>` for Baileys

[Compose data volume](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/docker-compose.yml#L193-L205) [Engine auth-directory shapes](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/engine/engine.factory.ts#L129-L195) [Backup scope](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/docs/10-devops-infrastructure.md#L1041-L1047)

## API-key behavior

On the first boot with no API-key rows, `API_MASTER_KEY` is used if set; otherwise OpenWA generates `owa_k1_` plus 32 random bytes encoded as hex. It creates an ADMIN key row whose raw key is hashed in `main.sqlite`, writes the raw bootstrap key to `data/.api-key` with owner-only permissions, and shows the full value in the first startup banner. Later boots mask it. The bootstrap file is an operator convenience, not an authentication source, and is removed if its key is revoked/deleted. API calls accept `X-API-Key` or Bearer authentication. [Seed generation and precedence](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/auth/auth.service.ts#L24-L53) [First-boot persistence](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/auth/auth.service.ts#L86-L128) [Key hashing/storage on creation](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/auth/auth.service.ts#L240-L277) [Accepted request credentials](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/auth/guards/api-key.guard.ts#L98-L124)

## Dashboard and LAN exposure

In production there is no dashboard service and no port `2886`. The production image bundles the SPA into `openwa-api`, served on the same port as the API: dashboard `/`, API `/api`, and (if enabled) Swagger `/api/docs`, all on container port `2785`. `2886` is only the Vite development server. [Bundled-dashboard Compose contract](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/docker-compose.yml#L234-L236) [Published port table](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/README.md#L263-L269) [Dashboard static-serving behavior](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/app.module.ts#L78-L94)

The shipped production Compose publishes `127.0.0.1:${API_PORT:-2785}:2785`, so it is loopback-only, **not** LAN-reachable. To administer directly from the LAN, replace that mapping in a local override with `192.168.1.250:2785:2785` (assuming that address is stable), then separately restrict TCP 2785 to the trusted LAN subnet in the host firewall. Binding `0.0.0.0` without the firewall would expose it on every laptop interface. If administration through an SSH tunnel is acceptable, retain the safer upstream loopback bind and no LAN port exposure is necessary. [Exact upstream bind](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/docker-compose.yml#L93-L98)

Because this design serves the dashboard directly over LAN HTTP rather than TLS, set `CSP_UPGRADE_INSECURE_REQUESTS=false`; otherwise the production default may upgrade dashboard asset requests to HTTPS and produce a blank UI. [Plain-HTTP dashboard warning](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/main.ts#L348-L377)

## Health contract

The Compose healthcheck executes a Node HTTP request to `http://localhost:2785/api/health/ready` every 30 seconds, with a 10-second timeout, three retries, and a 30-second start period. Readiness returns 200 only when both the main and data databases respond and the process is not draining; it returns 503 otherwise. `/api/health`, `/api/health/live`, and `/api/health/ready` are deliberately public and unthrottled. This checks service/database readiness, not whether the WhatsApp session is paired and ready; messaging readiness needs a separate session-status verification. [Compose healthcheck](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/docker-compose.yml#L218-L229) [Health endpoint semantics](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/health/health.controller.ts#L26-L105)

## Pinning and manual upgrades

The shipped production Compose has `build: .`, not an OpenWA image tag, so checking out exact tag `v0.12.1` and verifying commit `31c5499...` pins the built source. An image-based custom Compose can instead use `ghcr.io/rmyndharis/openwa:0.12.1` (or, strongest, a recorded digest), but must reproduce the required `/app/data` persistence and desired hardening/port contract. Do not use `latest`: upstream intentionally treats it as a mutable release channel. Release images are multi-arch for `linux/amd64` and `linux/arm64`; this laptop is `amd64`. [Shipped source build](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/docker-compose.yml#L45-L51) [Release-image example and volume requirement](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/docs/10-devops-infrastructure.md#L197-L242) [Release tag publication rules](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/.github/workflows/release.yml#L306-L321) [Published architectures](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/README.md#L258-L262)

## Engine-switch semantics

`ENGINE_TYPE` is deployment-wide, not per session. Valid values are exactly `whatsapp-web.js` and `baileys`; a typo is rejected at boot. Changing it through the dashboard saves `ENGINE_TYPE` to `data/.env.generated` and requires a server restart. A real process/Compose value takes precedence and therefore pins the engine against dashboard changes. [Engine validation](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/config/env.validation.ts#L62-L72) [Dashboard engine persistence](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/infra/infra.controller.ts#L861-L884) [Restart required](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/modules/infra/infra.controller.ts#L973-L980)

The engines do **not** share authentication state. whatsapp-web.js uses a Chromium `LocalAuth` profile under `data/sessions/session-<name>`; Baileys uses its own multi-file state under `data/baileys/<name>`. Therefore the first switch from whatsapp-web.js to Baileys requires a fresh QR scan/pairing for Baileys. OpenWA deliberately retains the inactive engine's auth directory, so switching back to a previously paired engine should restore its prior link without re-pairing, provided the credentials remain valid and neither the WhatsApp linked device nor session data was logged out/deleted. Deleting the OpenWA session purges both engines' directories. [Separate auth shapes and retained fallback credentials](https://github.com/rmyndharis/OpenWA/blob/31c5499a9beea1c5b460a4854ed68587b25f53d2/src/engine/engine.factory.ts#L129-L195)

Operationally: stop the session, back up `openwa-data`, change the pinned `ENGINE_TYPE`, restart OpenWA, start the same named session, and pair once if that engine has no valid auth directory. Never run both engines simultaneously for the same account. Roll back by stopping, restoring `ENGINE_TYPE=whatsapp-web.js`, restarting, and starting the same named session; the retained whatsapp-web.js directory is the rollback asset.
