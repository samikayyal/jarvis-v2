Type: task
Status: resolved
Blocked by: 03

## Question

Provision the locked minimal OpenWA runtime on the laptop, including persistent state, a generated API master key stored with restrictive permissions, LAN-only administration, restart-after-reboot behavior, and health verification. What was installed and where are its non-secret operational artifacts?

Never copy credentials or pairing/session material into this repository or the ticket.

## Comments

## Answer

Provisioned and verified on `sami-lenovo` on 2026-07-31:

- Installed Docker Engine 29.7.0 and Docker Compose 5.3.1 from Docker's official Ubuntu 26.04 (`resolute`) repository; Docker is enabled and active at boot.
- Added `/swapfile-openwa` (3.5 GiB) without deleting the existing 512 MiB swap file, for 4 GiB configured swap total; added it idempotently to `/etc/fstab`.
- Stopped XScreensaver/`glmatrix` and installed the user override `/home/samik/.config/autostart/lxqt-xscreensaver-autostart.desktop` with `Hidden=true`.
- Enabled UFW with default deny incoming/default allow outgoing. Preserved the pre-existing SSH allow rule, added SSH from `192.168.1.0/24`, and allowed TCP 2785 from `192.168.1.0/24` only. A fresh SSH connection succeeded after enablement.
- Pulled OpenWA `0.12.1` for `linux/amd64` and pinned the Compose deployment to immutable digest `sha256:c052dc03d3bfca490fa41f40e99aa13604239cef9c62c05f72762ef633fda85a`.
- Created root-owned `/opt/openwa/compose.yaml` (mode 0644) and `/opt/openwa/.env` (mode 0600). The environment contains distinct generated API master key and API-key pepper values; their values were not emitted or copied into this tracker.
- Created persistent Docker volume `openwa-data` at `/app/data`; OpenWA's generated sensitive files are mode 0600.
- Deployed only `openwa-api` with SQLite/local storage, `whatsapp-web.js`, automatic session restoration, `restart: unless-stopped`, and exact host bind `192.168.1.250:2785`.
- Verified container health, authenticated `GET /api/sessions` HTTP 200, dashboard HTTP 200 from another LAN machine, readiness HTTP 200, persistent volume creation, and the exact image digest/restart/bind configuration.

At pre-pairing steady state the container used about 227 MiB RAM and roughly 1.35% CPU in a point-in-time sample. Host available memory was about 2.5 GiB. Actual Chromium/session usage must be measured after pairing.

Operator URL: `http://192.168.1.250:2785`

No API key, pepper, QR code, or WhatsApp auth state is stored in this repository.
