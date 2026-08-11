# Jarvis Assistant V1 unactivated bundle

This directory is a reviewed, offline-verifiable deployment description. Every
container is behind the `manual-activation` Compose profile. Running the verifier
does not build or start a container, install either native worker, provision a
credential, create or attach a network, change a firewall, or contact OpenWA.

Verify from the repository root:

```text
uv run python -m jarvis_control_plane.deployment deployment
docker compose --file deployment/compose.yaml config --quiet
```

The second command parses Compose only. Do not pass `--profile
manual-activation` or run `up` during isolated verification.

The broker plus isolated deleted archive and each credentialed connector plus
its uncredentialed egress sidecar share the corresponding fixed V1 component
budget. The complete Compose project therefore retains the specified aggregate
limits of 1,008 MiB RAM, 1.80 CPU cores, and 512 PIDs.

The image entry point is the role-specific runtime, not the offline verifier.
Each Compose service selects exactly one composition root and exposes only its
closed operation set. Owned-service calls use bounded JSON frames authenticated
in both directions with a distinct HMAC key for each client-to-server link. The
offline verifier compares the exact network membership and network mode of every
service with this reviewed topology, including the networkless deleted archive.

The inbound HTTP role verifies the exact signed OpenWA body and waits only for
the broker's durable ingress admission. It then returns the admission disposition
while a broker-owned background worker claims and processes the message outside
the webhook lifetime; model turns, connector calls, terminal work, and outbound
delivery never hold the webhook request open.

`config.example.toml` contains no real operator, Google, OpenWA, vault, or worker
identity. Before a later supervised activation, a manual administrator must make
a root-owned `/etc/jarvis/jarvis.toml` in mode `0444` from the reviewed schema,
set `configuration_kind = "active"`, create only the service-specific credential
directories described below, and independently review the resulting exact
values. Credential material never belongs in this repository;
`deployment/credentials/` is explicitly ignored as a defense against accidental
staging.

| Service | Credential boundary |
| --- | --- |
| inbound receiver | `/run/credentials/openwa-inbound/credentials.json`: receiver-scoped `openwa_signing_secret` used over the exact raw webhook body before forwarding |
| capability broker | `/run/credentials/broker/credentials.json`: `openwa_signing_secret` |
| orchestration agent | `/run/credentials/openai` read-only |
| Google connector | `/run/credentials/google/credentials.json`: `client_id`, `client_secret`; private writable directory also owns OAuth state and refresh token |
| knowledge-vault connector | `/run/credentials/vault`: pinned `ssh_config` and `known_hosts` |
| OpenWA outbound connector | `/run/credentials/openwa/credentials.json`: `api_base_url`, `api_key` |
| worker gateway | `/run/credentials/windows-worker/credentials.json`: reviewed Ubuntu peer/socket UIDs and connection ID plus Windows connection, certificate/application identities, and one non-wildcard overlay bind address/port; same directory contains the private worker CA and gateway certificate/key |

The OpenAI credential document is `credentials.json` with one `api_key`. Each
credential directory and file is provisioned by the manual administrator for
only its numeric service UID: directories use `0700` and credential files use
`0600`. Protocol keys are root-owned, group `20000`, mode `0440`; the shared
protocol group grants readability only inside containers where an exact key is
separately mounted, never by path discovery.
Each file under `/etc/jarvis/protocol` is named
`<client-role>--<server-role>.key`, contains at least 32 random bytes, and is
mounted only into those two roles. A server selects the key by the claimed
client identity and verifies the frame before admitting the operation, so a
key for a read-only link cannot impersonate the capability broker.

The orchestration image includes the reviewed `@openai/codex` CLI version from
`artifacts.lock.json`; `codex/package-lock.json` verifies the package and
platform artifact integrity during `npm ci`. The orchestration role mounts exactly one Git workspace
at `/srv/jarvis-workspace` read-only and keeps its Codex traces in the
UID-10003-owned `/var/lib/jarvis/codex-traces` directory. Codex receives the
same service-scoped OpenAI API key as the orchestration SDK, but it receives no
connector, worker, deployment, or activation credential.
The Agents SDK model turn is cancelled at `timeouts.model_turn_seconds`, and
the broker's authenticated orchestration link exposes a separate cancellation
operation so `/cancel` also reaches an active remote SDK or Codex run. The Codex
subprocess receives only its API key, basic process environment, and the reviewed
`HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` route variables.

The native Ubuntu and Windows workers are not Compose services. Before activation,
a root administrator creates `/run/jarvis-worker`, starts the reviewed native
Ubuntu worker so its mode-`0600` socket is present there, and verifies ownership
against the configured peer UID. Compose mounts that exact socket read-only and
the worker gateway consumes the reviewed authenticated Ubuntu socket and registers the
outbound Windows transport, but this bundle does not install either worker,
provision identities or certificates, or alter either host. OpenWA is not a
service in this Compose project. Manual activation attaches the independent
OpenWA API and `openwa_outbound_connector` to the externally named, reviewed
`jarvis-openwa-api` route. This API route is distinct from the future two-member
inbound handoff documented in `openwa-handoff.md`.

The worker gateway listens for the Windows worker only on the exact configured
private-overlay address (port `9443` in the reviewed Compose metadata), requires
TLS 1.3 client authentication and the registered certificate/application
identity, and attaches the worker-initiated session to the gateway transport.
The native Windows service runs `run_windows_worker_client` with its existing
Job Object executor. Correlated request IDs let execution and cancellation share
that single outbound session concurrently, so Job Object termination is not
blocked behind the execution response. Installation and activation remain
manual-only.

The callback process intentionally binds plain HTTP only on host loopback. A
separately reviewed host TLS reverse proxy must terminate the exact configured
HTTPS callback URL and route only `/callback` to `127.0.0.1:8080`; it must not
expose any private service port. That proxy is an activation prerequisite, not
an unreviewed Compose service.

Google authorization is initiated only by a manual administrator through the
authenticated broker-to-Google service link; it is not exposed on the callback
endpoint or to the orchestration model. With the Google connector running, use:

```text
docker compose --file deployment/compose.yaml run --rm capability_broker google-authorize --operation-id <reviewed-id>
docker compose --file deployment/compose.yaml run --rm capability_broker google-authorize --operation-id <reviewed-id> --access gmail-send
docker compose --file deployment/compose.yaml run --rm capability_broker google-authorize --operation-id <reviewed-id> --access calendar-write
docker compose --file deployment/compose.yaml run --rm capability_broker google-disconnect
```

The first command prints the single-use baseline Google consent URL. Run the
matching `--access gmail-send` or `--access calendar-write` command before
approving an action that needs that write capability. Each incremental flow
retains the already reviewed grant scopes and adds only its named write scope.
Every grant includes `openid` so the connector can bind the returned OpenID
subject to the configured identity. The final command revokes and removes the
current grant.

Safe audit inspection stays inside the UID-10004 audit boundary. A local
administrator can run `audit-view` or `audit-export` as a one-off
`audit_service` command and capture stdout; neither command is available through
the broker protocol.

For a content-free aggregate view after activation, a local administrator can
run `uv run python -m jarvis_control_plane.deployment deployment
--administrative-status`. The host-side command reads Compose health locally and
uses a one-off broker-identity process only for authenticated messaging, audit,
and worker readiness. It reports no credentials or personal identifiers. Backup
freshness is calculated on the host as `missing`, `current`, `stale`, or `invalid`
from the local snapshot manifests without exposing backup contents.

## Administrative backup and isolated restore

Run the backup command as the root administrator so SQLite can take online,
transactionally consistent copies and the restore can preserve the original
owners and modes. The fixed database inventory covers Jarvis state and sessions,
append-only audit, broker/Codex/Google diagnostic traces, and the deleted-
conversation archive. The reviewed configuration, SQLite schema hashes, and
artifact release metadata travel with every snapshot. Credentials, private keys,
OpenWA state, caches, the knowledge-vault clone, and external authoritative
content cannot enter the snapshot because they are not accepted inputs.

The unactivated bundle ships reviewed `jarvis-backup.service` and
`jarvis-backup.timer` units for a persistent nightly run. Installing and enabling
those units remains a manual activation step. Run the same command immediately
before an upgrade, active-configuration change, or migration, varying only the
required kind:

```console
uv run python -m jarvis_control_plane.administrative_backup create --kind nightly --artifact-lock /opt/jarvis/current/deployment/artifacts.lock.json
uv run python -m jarvis_control_plane.administrative_backup create --kind pre-change --artifact-lock /opt/jarvis/current/deployment/artifacts.lock.json
```

Both commands create a new mode-`0700` versioned directory under
`/var/backups/jarvis`; they never replace or automatically remove a snapshot.
The administrator must provision that backup root outside every Jarvis-readable
path. Installing or enabling the shipped timer is intentionally not performed by
the bundle.

Restore always targets a path that does not yet exist. It verifies every checksum,
the complete database inventory and schema, SQLite integrity, audit readability,
owners and modes, and compatible configuration/release metadata before publishing
the isolated result:

```console
uv run python -m jarvis_control_plane.administrative_backup restore /var/backups/jarvis/SNAPSHOT /var/lib/jarvis-restore/rehearsal --artifact-lock /opt/jarvis/current/deployment/artifacts.lock.json
```

The restored tree is rehearsal material only. The command does not stop, start,
reconfigure, migrate, or otherwise activate any Jarvis or OpenWA service.

Orchestration, Google, and vault services have no direct Internet-routed
network. Each reaches only its dedicated CONNECT proxy on a private segment;
only the three uncredentialed proxies join `external_egress`, and each proxy
admits the exact active host allowlist (HTTPS port 443, or SSH port 22 for the
vault). The vault adapter additionally forces Git SSH through its proxy command.

Fresh persistent directories are also a manual activation prerequisite. Create
the state/trace paths for UID 10002, Codex traces for UID 10003, audit for UID
10004, Google trace and credential state for UID 10005, the vault clone for UID
10006, and the deleted-conversation archive for UID 10010, all with the reviewed
`0700` directory mode. Create `/run/jarvis/deleted-archive-ipc` for group 20000
with mode `0770`. Only the broker's write-only authenticated archive client and
the UID-10010 archive service mount that IPC directory; only UID 10010 mounts
the retained archive database. The long-lived services never start as root and
do not repair ownership themselves.

The image build is reproducible from the Git-pinned application artifact, the
digest-pinned Python and Node build bases, the pinned Codex CLI, and the
hash-locked exported requirements. Building,
publishing, credential provisioning, worker installation, OpenWA attachment,
and activation are separate manual-administration steps.
