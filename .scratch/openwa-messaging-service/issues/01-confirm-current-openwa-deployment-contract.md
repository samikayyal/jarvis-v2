Type: research
Status: resolved
Blocked by:

## Question

At the current pinned OpenWA revision, what exact Compose services, environment variables, persistent paths, API-key behavior, dashboard exposure, health checks, and engine-switch semantics are required for a minimal single-session `whatsapp-web.js` deployment with a Baileys fallback?

## Comments

## Answer

Use the pinned OpenWA `v0.12.1` production contract documented in [the primary-source research artifact](../research/openwa-v0.12.1-deployment-contract.md): default Compose profile, `openwa-api` plus its shipped Docker proxy, SQLite/local storage on the single `openwa-data` volume, bundled dashboard and API together on port 2785, explicit LAN bind/firewall or an SSH tunnel, API key and pepper set from first boot, and `whatsapp-web.js` as the pinned engine. Baileys uses a separate auth directory, so its first activation requires pairing; switching back can reuse the retained whatsapp-web.js auth state.
