# OpenWA deployment and security contract

## Pinned runtime

| Component | Contract |
| --- | --- |
| OpenWA | Release `v0.12.1`, upstream commit `31c5499a9beea1c5b460a4854ed68587b25f53d2` |
| Container image | `ghcr.io/rmyndharis/openwa@sha256:c052dc03d3bfca490fa41f40e99aa13604239cef9c62c05f72762ef633fda85a` |
| Host architecture | `linux/amd64` |
| Docker Engine | 29.7.0, enabled at boot |
| Docker Compose | 5.3.1 |
| Service set | `openwa-api` only |
| Messaging engine | `baileys` only |
| Database | SQLite |
| Media storage | Local persistent volume |
| Restart policy | `unless-stopped` |
| Stop grace | 45 seconds |

Do not track `latest`, a moving branch, or an unrecorded image tag. Upgrades are
manual changes that must be followed by the verification sequence in the
[operations runbook](operations.md).

## Host posture

The gateway runs on a low-resource Ubuntu 26.04 LTS laptop with four CPU cores,
3.7 GiB usable RAM, and rotational storage. Provisioning added a 3.5 GiB swap
file alongside the original 512 MiB file, for 4 GiB total configured swap.
Both swap files are boot-persistent.

The graphical environment remains installed, but the CPU-heavy
XScreensaver/`glmatrix` autostart was disabled. Lid-close behavior is configured
to keep the host running, and the private network connection uses a static,
autoconnecting address.

## Deployment layout

| Artifact | Location and rule |
| --- | --- |
| Compose file | `/opt/openwa/compose.yaml`, root-owned, mode 0644 |
| Environment and secrets | `/opt/openwa/.env`, root-owned, mode 0600 |
| Persistent state | Docker volume `openwa-data`, mounted at `/app/data` |
| API-key database | `/app/data/main.sqlite` |
| Application database | `/app/data/openwa.sqlite` |
| Generated configuration | `/app/data/.env.generated` |
| Bootstrap API-key file | `/app/data/.api-key`; sensitive operator convenience, not the authentication source |
| Local media | `/app/data/media` |
| whatsapp-web.js auth root | `/app/data/sessions` |
| Baileys auth root | `/app/data/baileys` |

`main.sqlite` and `openwa.sqlite` are separate databases and must never be
configured to the same path. The `openwa-data` volume is the authoritative
persistence boundary: losing it loses databases, API-key state, media, and
WhatsApp pairings.

## Application configuration

The durable Compose contract is:

- production mode;
- SQLite at `/app/data/openwa.sqlite` with schema synchronization disabled;
- local media at `/app/data/media`;
- Redis, queueing, external cache, PostgreSQL, and MinIO disabled;
- `ENGINE_TYPE=baileys`;
- `BAILEYS_AUTH_DIR=/app/data/baileys`;
- `SESSION_DATA_PATH=/app/data/sessions` retained for a reversible engine switch;
- automatic session startup enabled;
- bundled dashboard and API served by the same container;
- Swagger disabled;
- plain-LAN-HTTP CSP behavior retained;
- `restart: unless-stopped` and `stop_grace_period: 45s`.

The environment contains distinct high-entropy API master-key and API-key
pepper values. Never print, copy, or commit either value. A real Compose/process
environment takes precedence over OpenWA's generated environment, so the engine
and other locked production choices must remain pinned in Compose.

On first boot OpenWA hashes the bootstrap API key into `main.sqlite` and may
retain its raw value in the mode-0600 `.api-key` file. Treat that file as a
secret and never expose it in diagnostics. Changing the API-key pepper later
invalidates existing key hashes.

## Network boundary

The container publishes port 2785 only on the laptop's exact private LAN
address, not on `0.0.0.0`. UFW is active with default-deny inbound behavior and
allows OpenWA only from the trusted home `/24`. SSH access is preserved
separately.

This is defense in depth: Docker-published ports may bypass ordinary UFW
forwarding expectations, so the exact private-address bind is the primary
exposure boundary. Do not add public router forwarding or a public bind as part
of this deployment.

## Health and readiness

OpenWA's container health check targets `/api/health/ready`. It proves that the
process and both databases are responsive and the app is not draining. It does
not prove that WhatsApp is paired or connected.

The health endpoints are intentionally unauthenticated. They must reveal only
service readiness and must not be mistaken for an authenticated operational or
messaging-status API.

Operational readiness therefore requires both:

1. container health `healthy`; and
2. the named WhatsApp session status `ready` through the authenticated API.

Any automation or future assistant must preserve this distinction.

## Excluded infrastructure

This deployment intentionally does not run PostgreSQL, Redis, MinIO, Traefik,
the optional Docker proxy, a local LLM, multiple accounts, or concurrent OpenWA
sessions. Adding any of them is a new architecture decision, not routine
maintenance.
