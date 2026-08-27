# Jarvis Assistant V1 unactivated bundle

The supervised production procedure is documented in
[`activation-runbook.md`](activation-runbook.md). Do not use that runbook until
this unactivated bundle, its exact release pins, and every manual trust boundary
have been independently reviewed.

After activation is complete, use the separately supervised
[`google-vault-acceptance-runbook.md`](google-vault-acceptance-runbook.md) for
the real-system checks required by implementation ticket 31.

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

The Agents SDK model turn is cancelled at `timeouts.model_turn_seconds`, and
the broker's authenticated orchestration link exposes a separate cancellation
operation so `/cancel` also reaches an active remote SDK run.

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
docker compose --file deployment/compose.yaml run --rm capability_broker google-disconnect
```

The first command prints the single-use baseline Google consent URL. Run the
matching `--access gmail-send` command before approving an action that needs
that write capability. Each incremental flow retains the already reviewed v1
grant scopes, adds Gmail send, and drops any legacy Calendar scope. Jarvis v1
does not request or expose Calendar access.
Every grant includes `openid` so the connector can bind the returned OpenID
subject to the configured identity. The final command revokes and removes the
current grant.

Safe audit inspection stays inside the UID-10004 audit boundary. A local
administrator can run `audit-view` or `audit-export` as a one-off
`audit_service` command and capture stdout; neither command is available through
the broker protocol.

For a content-free aggregate view after activation, a local administrator can
run `uv run python -m jarvis_control_plane.deployment deployment
--administrative-status --activation-override /etc/jarvis/activation.compose.yaml
[--backup-root /configured/backup/root]`. The host-side command reads the exact
combined active Compose model and executes a read-only status probe inside the
active broker for authenticated messaging, audit, and worker readiness. It
reports no credentials or personal identifiers. Backup
freshness is calculated on the host as `missing`, `current`, `stale`, or `invalid`
from the local snapshot manifests without exposing backup contents.

After activation, run real Google and vault checks from
`google-vault-acceptance-runbook.md`. Run authenticated Ubuntu, Windows, and
terminal-authority checks from `terminal-acceptance-runbook.md`. Both are human-supervised worksheets;
passing controlled tests is not production acceptance and neither worksheet
authorizes deployment or trust-critical activation.

After both worksheets are complete, use `ticket33-acceptance-runbook.md` for
the final endurance, degraded-mode, backup, upgrade, rollback, and full-host
reboot acceptance. Its disruptive rows require a dedicated maintenance window.

## Administrative backup and isolated restore

Run the backup command as the root administrator so SQLite can take online,
transactionally consistent copies and the restore can preserve the original
owners and modes. The fixed database inventory covers Jarvis state and sessions,
append-only audit, broker and Google diagnostic traces, and the deleted-
conversation archive. The reviewed configuration, SQLite schema hashes, and
artifact release metadata travel with every snapshot. The active `compose.yaml`
and adjacent `image-digests.json` also travel with it; the digest file maps every
Compose service name to its activated `image@sha256:...` reference. Credentials, private keys,
OpenWA state, caches, the knowledge-vault clone, and external authoritative
content cannot enter the snapshot because they are not accepted inputs.

The unactivated bundle ships reviewed `jarvis-backup.service` and
`jarvis-backup.timer` units for a persistent nightly run. Installing and enabling
those units remains a manual activation step. Before installing them, create the
service environment from the shipped hash-locked requirements; the timer never
resolves or downloads dependencies:

```console
JARVIS_HOST_PYTHON=/opt/jarvis/python/cpython-3.13.13-linux-x86_64-gnu/bin/python3.13
test -x "$JARVIS_HOST_PYTHON"
uv venv --python "$JARVIS_HOST_PYTHON" /opt/jarvis/current/.venv
uv pip install --python /opt/jarvis/current/.venv/bin/python --require-hashes -r /opt/jarvis/current/deployment/requirements.lock
systemd-run --wait --collect --unit=jarvis-python-mdwe-preflight \
  --uid=jarvis-worker --gid=jarvis-worker \
  --property=MemoryDenyWriteExecute=yes \
  /opt/jarvis/current/.venv/bin/python -c pass
```

The host runtime patch version must match the pinned `python_base_image` version
in `artifacts.lock.json`. The hardening preflight must finish successfully before
installing or restarting either native-worker service; a root-private uv runtime
or a runtime that faults under `MemoryDenyWriteExecute=yes` is not releasable.

During manual activation, record the exact activated `image@sha256:...` value
for every Compose service in `/etc/jarvis/image-digests.json`, then make the
file root-owned and mode `0600`. The shipped timer passes that explicit file to
the backup command, which compares every recorded digest with the active
container image ID before publishing a snapshot.

Run the same installed Python immediately before an upgrade, active-
configuration change, or migration, varying only the required kind:

```console
/opt/jarvis/current/.venv/bin/python -m jarvis_control_plane.administrative_backup create --kind nightly --artifact-lock /opt/jarvis/current/deployment/artifacts.lock.json --compose-manifest /opt/jarvis/current/deployment/compose.yaml --image-digests /etc/jarvis/image-digests.json
/opt/jarvis/current/.venv/bin/python -m jarvis_control_plane.administrative_backup create --kind pre-change --artifact-lock /opt/jarvis/current/deployment/artifacts.lock.json --compose-manifest /opt/jarvis/current/deployment/compose.yaml --image-digests /etc/jarvis/image-digests.json
```

Both commands create a new mode-`0700` versioned directory under
`/var/backups/jarvis`; they never replace or automatically remove a snapshot.
The administrator must provision that backup root outside every Jarvis-readable
path. Installing or enabling the shipped timer is intentionally not performed by
the bundle.

Restore always targets a path that does not yet exist beneath an already
provisioned, root-owned directory that is not group/world-writable. It verifies every checksum,
the complete database inventory and schema, SQLite integrity, audit readability,
owners and modes, and compatible configuration/release metadata before publishing
the isolated result:

```console
/opt/jarvis/current/.venv/bin/python -m jarvis_control_plane.administrative_backup restore /var/backups/jarvis/SNAPSHOT /var/lib/jarvis-restore/rehearsal --artifact-lock /opt/jarvis/current/deployment/artifacts.lock.json
```

The restored tree is rehearsal material only. The command does not stop, start,
reconfigure, migrate, or otherwise activate any Jarvis or OpenWA service.

## Pinned upgrade and rollback rehearsal

After the pre-change backup and maintenance admission stop, rehearse the exact
previous and replacement release directories in a new private workspace. Each
directory must contain its own immutable `deployment/artifacts.lock.json`; both
bundles and the active configuration are validated before state is restored.
The bounded message window must contain every unfinished outbound attempt:

```console
PYTHONPATH=/opt/jarvis/replacement/src /opt/jarvis/replacement/.venv/bin/python -m jarvis_control_plane.upgrade_rehearsal /opt/jarvis/previous /opt/jarvis/replacement /var/backups/jarvis/SNAPSHOT /var/lib/jarvis-rehearsal/20260812T080000Z --configuration /etc/jarvis/jarvis.toml --admission-stopped-at 2026-08-12T07:59:00+00:00 --window-start 2026-08-12T07:55:00+00:00 --window-end 2026-08-12T08:05:00+00:00 --history-export /var/lib/jarvis-rehearsal/openwa-history.json
```

The command must run with the replacement release's installed Python and rejects
another runtime. It requires the admission-stop timestamp and verifies that the
snapshot is a later pre-change backup of the exact previous artifact lock. The
rehearsal initializes the replacement release only against the isolated restore.
The required bounded OpenWA history export is a JSON array of non-secret
`session_id`, `message_id`, `event_id`, and timezone-aware `occurred_at` values;
it lets the rehearsal record messages missing from the restored inbox under the
durable deduplication key. The rehearsal interrupts unfinished requests and
pending actions, and closes
known-unattempted or attempted-but-unconfirmed dispatch and outbound work as
`not_started` or `unknown` without replaying either.
Use `--force-failure` to exercise rollback; the command then restores the same
pre-change snapshot using the previous release's compatible artifact lock.
The command creates files only below the new rehearsal workspace. It never
invokes Compose or systemd, changes active configuration or data, activates a
release, or changes OpenWA.

Orchestration, Google, and vault services have no direct Internet-routed
network. Each reaches only its dedicated CONNECT proxy on a private segment;
only the three uncredentialed proxies join `external_egress`, and each proxy
admits the exact active host allowlist (HTTPS port 443, or SSH port 22 for the
vault). The vault adapter additionally forces Git SSH through its proxy command.

Fresh persistent directories are also a manual activation prerequisite. Create
the state/trace paths for UID 10002, audit for UID 10004, Google trace and
credential state for UID 10005, the vault clone for UID
10006, and the deleted-conversation archive for UID 10010, all with the reviewed
`0700` directory mode. Create `/run/jarvis/deleted-archive-ipc` for group 20000
with mode `0770`. Only the broker's write-only authenticated archive client and
the UID-10010 archive service mount that IPC directory; only UID 10010 mounts
the retained archive database. The long-lived services never start as root and
do not repair ownership themselves.

The image build is reproducible from the Git-pinned application artifact, the
digest-pinned Python build base, and the hash-locked exported requirements. Building,
publishing, credential provisioning, worker installation, OpenWA attachment,
and activation are separate manual-administration steps.
