# OpenWA verification record

Verification date: 2026-07-31.

## Host readiness and provisioning

The initial inspection found suitable CPU architecture, disk capacity, static
LAN addressing, SSH, and outbound registry/WhatsApp access. It also found no
Docker installation, only 512 MiB fully occupied swap, inactive UFW, and an
unnecessarily CPU-heavy screensaver.

Provisioning then established the current contract:

- Docker Engine and Compose installed from Docker's official Ubuntu repository;
- Docker enabled at boot;
- 4 GiB total configured swap;
- screensaver workload stopped and disabled through a user autostart override;
- UFW enabled with LAN-only OpenWA access;
- immutable OpenWA image digest pinned;
- root-owned Compose and mode-0600 environment files;
- one persistent `openwa-data` volume;
- authenticated API, dashboard, health, bind, and restart policy verified.

Before pairing, the web-engine container used about 227 MiB RAM and 1.35% CPU
in one point-in-time sample. Initial Chromium synchronization temporarily raised
usage to about 752 MiB; that was not treated as steady state.

## whatsapp-web.js trials

The dedicated account was paired twice with `whatsapp-web.js`:

1. The first run used OpenWA's default remotely pinned WhatsApp Web build. It
   reached `ready`, and inbound/outbound text messaging passed. WhatsApp then
   issued `LOGOUT` before the planned restart test.
2. The deployment opted out of that remote build with
   `WWEBJS_WEB_VERSION=off`, using WhatsApp's first-party served build. The
   second pairing reached `ready`, but WhatsApp issued another `LOGOUT` after
   roughly three minutes without a service restart.

In both cases OpenWA correctly removed the credentials WhatsApp had invalidated.
The failures therefore were not caused by Docker restarts, volume loss, or the
selected web-build source. No repeated web re-pairing was attempted after the
second invalidation.

## Baileys acceptance result

Baileys was activated as the only engine with its separate persistent auth
root. Acceptance checks demonstrated:

- container health `healthy` and session state `ready`;
- a clean multi-minute pre-restart stability window with no `LOGOUT` or failed
  events;
- exact inbound text persisted as `incoming/text`;
- an authenticated API reply persisted as `outgoing/sent` and confirmed by the
  user on the receiving phone;
- `stop_grace_period: 45s` accepted by Compose and applied by Docker;
- a controlled Compose recreation returning directly to `healthy` and `ready`;
- retained Baileys auth state with no new QR;
- two further healthy/ready post-restart samples;
- exact private-address publication, active UFW policy, and `unless-stopped`
  restart policy still present.

The outbound row remained `sent` rather than being promoted to `delivered` in
OpenWA's database, but physical receipt was confirmed. This is sufficient for
the completed transport check and should not be misreported as a recorded
delivery receipt.

## Settled resource envelope

After the Baileys recreation and synchronization settled, three samples showed:

| Measure | Result |
| --- | --- |
| Container CPU | 0.01% in all three samples |
| Container RAM | 132.0-132.6 MiB |
| Container share of host RAM | 3.48-3.50% |
| Host memory available | 2.6 GiB |
| Swap free | 2.5 GiB of 4.0 GiB |

These measurements support the selected single-session Baileys deployment on
the 4 GB-class laptop. They do not establish capacity for a local LLM or the
excluded infrastructure services.

## Remaining acceptance boundary

A full laptop reboot was not performed. Boot recovery is expected from Docker
boot enablement, `restart: unless-stopped`, automatic session startup, and the
successful controlled persistence test, but a future planned maintenance window
may verify the complete host-reboot path separately.

The detailed evidence and chronology remain in the
[resolved Wayfinder tickets](../../.scratch/openwa-messaging-service/map.md).
