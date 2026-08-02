Type: grilling
Status: resolved
Blocked by: 01, 02

## Question

Given the verified upstream deployment contract and live host capacity, what exact pinned revision, engine, Compose profile, service set, resource posture, persistent-data layout, LAN binding/firewall policy, and swap requirement should govern the installation?

## Comments

## Answer

Use OpenWA release `v0.12.1` on the official `linux/amd64` container image, recording the pulled immutable digest during provisioning. Run only `openwa-api`: SQLite, local media, memory cache, no queue, no PostgreSQL/Redis/MinIO/docker-proxy, one persistent Docker volume at `/app/data`, `restart: unless-stopped`, `whatsapp-web.js`, automatic session restoration, and manual upgrades only.

The host runtime prerequisites are Docker Engine plus the Compose plugin from Docker's official Ubuntu 26.04 repository, an additional 3.5 GiB swap file alongside the existing 512 MiB file, and removal of the active `glmatrix` screensaver from the graphical session's autostart.

Publish OpenWA as `192.168.1.250:2785:2785` so every device on `192.168.1.0/24` can reach the dashboard/API but the service is not bound to loopback, future interfaces, or every address. Enable UFW only after allowing SSH and TCP 2785 from `192.168.1.0/24`; deny other inbound traffic by default. Because Docker-published ports can bypass UFW, the exact private-address bind is the primary exposure boundary.

Store the Compose file in `/opt/openwa/compose.yaml`, secrets in root-owned mode-0600 `/opt/openwa/.env`, and state in the `openwa-data` volume. Generate distinct high-entropy `API_MASTER_KEY` and `API_KEY_PEPPER` before first boot and never record their values in this tracker.
