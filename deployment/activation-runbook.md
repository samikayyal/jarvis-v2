# Ticket 30 supervised activation runbook

This runbook governs the first activation of the pinned Jarvis Assistant V1
release and the single controlled OpenWA handoff. It is an operator procedure,
not an automation entry point. Every trust-critical change is performed and
reviewed by a human administrator.

Ticket 30 is complete only when the three acceptance checks in
`.scratch/jarvis-assistant-v1-implementation/issues/30-supervise-initial-activation-and-openwa-handoff-acceptance.md`
have current, sanitized production evidence. A passing automated suite or a
successful Compose start is not production acceptance.

## Safety rules

- Use a scheduled maintenance window with the authorized operator available on
  the dedicated WhatsApp account.
- Run host commands from a root shell on the appropriate Ubuntu host. Never put
  secrets, QR data, phone numbers, chat IDs, message bodies, tokens, or private
  keys on a command line that will be recorded.
- Keep OpenWA independently deployed. Do not copy its state into Jarvis or add it
  to the Jarvis Compose project.
- Do not edit `deployment/compose.yaml` during activation. The reviewed bundle
  intentionally excludes the production inbound handoff. Apply the separately
  reviewed, root-owned activation override described below.
- Do not treat OpenWA container health as WhatsApp readiness. Acceptance requires
  both container `healthy` and the configured named session `ready`.
- Do not retry an outcome-unknown outbound operation.
- Do not repeatedly recreate or re-pair OpenWA after `LOGOUT`.
- Keep all evidence content-free and sanitized. Record hashes, modes, identities
  by approved labels, status values, counts, timestamps, and pass/fail outcomes;
  never record secret values or personal identifiers.

## Immediate stop conditions

Stop activation or acceptance immediately if any of the following occurs:

- WhatsApp reports `LOGOUT`, a persistent disconnect, or `failed`.
- A controlled OpenWA recreation unexpectedly requires a new QR.
- An operator, OpenWA session, Google, vault, Ubuntu worker, or Windows worker
  identity differs from its independently reviewed value.
- A secret or personal identifier appears in Git, an image, rendered Compose,
  ordinary state, logs, command history, or acceptance evidence.
- A port is published on `0.0.0.0`, a private network has an unexpected member,
  or a new LAN, overlay, firewall, router, or public exposure exists.
- Required audit evidence cannot be appended, a side effect is duplicated, or an
  external outcome is unexplained.
- OpenWA no longer satisfies the pinned deployment contract, Jarvis exceeds its
  reviewed resource bounds, or either deployment enters a restart loop.

On a stop condition, preserve current state and a sanitized chronology, stop the
new Jarvis release if safe, and use the rollback section. Do not delete OpenWA
credentials, delete Docker volumes, repeat pairing, or conceal partial results.

## Roles and evidence record

Assign these roles before the window. One person may fill more than one role,
but every gate still needs an explicit review.

| Role | Responsibility | Assigned operator |
| --- | --- | --- |
| Activation operator | Performs the host changes and reads secret files without displaying them | |
| Independent reviewer | Confirms pins, identities, permissions, topology, and each gate | |
| WhatsApp verifier | Sends the unique inbound phrase and confirms the single outbound receipt | |
| Rollback decision owner | Calls stop/rollback when a gate fails | |

Create a private administrative evidence record outside every Jarvis-readable
path. Start it with:

| Field | Value |
| --- | --- |
| Maintenance window | |
| Release bundle path | `/opt/jarvis/current` |
| Release Git commit | |
| `artifacts.lock.json` SHA-256 | |
| Locked application revision | |
| Locked application source SHA-256 | |
| OpenWA image digest | |
| Jarvis image references/digests | |
| Pre-change Jarvis snapshot | |
| Protected OpenWA backup reference | |
| Previous compatible Jarvis release | |
| Activation operator | |
| Independent reviewer | |
| Rollback decision owner | |

## Phase 0: resolve prerequisites before scheduling

Do not schedule activation until every row passes.

- [ ] Ticket 29 is `complete`, including its forced-failure rollback rehearsal.
- [ ] The exact reviewed release is installed at `/opt/jarvis/current`; it is not
  a mutable working tree and is root-owned.
- [ ] The target host reports Docker Engine 29.7.0 and Docker Compose 5.3.1, as
  required by the pinned OpenWA deployment contract. Do not substitute the local
  workstation's Compose result for this host gate.
- [ ] Every Jarvis service image was reproducibly built or fetched before the
  window, and its immutable local image ID is known. Activation will use
  `--no-build --pull never`.
- [ ] The previous compatible Jarvis release and its compatible state are
  available for rollback.
- [ ] The reviewed native Ubuntu systemd unit and Windows SCM service are
  installed with manual activation state. The Ubuntu service must be disabled
  and inactive; the Windows service must use Manual startup and be stopped.
- [ ] A host TLS reverse-proxy configuration exposes only the exact public Google
  OAuth `/callback` path and routes it to `127.0.0.1:8080`.
- [ ] The OpenWA administrator has a reviewed persistent Compose change that adds
  the handoff network without changing the pinned image, engine, volume, port,
  restart policy, or 45-second stop grace.
- [ ] The authorized operator can observe the dedicated WhatsApp account and is
  prepared to send one conspicuously unique test phrase.
- [ ] The backup roots and evidence path are outside all Jarvis-readable paths.
- [ ] The rollback owner has reviewed the rollback commands and decision points.

### Prepared pre-wizard state for `sami-lenovo`

The non-activating preparation pass may leave the following root-only artifacts
for independent review. Their presence is not activation authorization:

| Prepared artifact | Purpose |
| --- | --- |
| `/etc/jarvis/jarvis.toml.pending` | Real OpenWA, callback, vault, and host values; remains `configuration_kind = "prepared"` until the Google subject is supplied and all identities are reviewed |
| `/etc/jarvis/activation.compose.yaml.pending` | Exact reviewed activation override, not yet installed at the active path |
| `/etc/jarvis/image-digests.json.prepared` | The 13 immutable prebuilt image IDs for pre-window comparison; activation still records the running IDs |
| `/etc/jarvis/credentials/openwa-inbound` and `broker` | One generated webhook HMAC shared only across the signed inbound boundary |
| `/etc/jarvis/credentials/openwa` | Existing OpenWA API credential copied internally without displaying it |
| `/etc/jarvis/credentials/vault` | Dedicated GitHub deploy key, pinned `known_hosts`, and SSH configuration; the public key still requires human registration |
| `/etc/jarvis/credentials/windows-worker` | Dedicated CA, gateway certificate/key, and closed registration using reviewed native worker UID `10008` |
| `/var/backups/jarvis-ticket30-windows-worker` | Root-only staged Windows client certificate/key and private CA recovery material, outside Jarvis-readable paths |
| `/etc/jarvis/native/ubuntu-worker.json` | Worker-owned mode-`0400` Ubuntu listener identity and exact gateway UID, inside a root-owned restricted directory |
| `jarvis-ubuntu-worker.service` | Installed, disabled, and inactive systemd unit for the UID-`10008` host worker |
| `JarvisWindowsWorker` | Installed, Manual, and stopped Windows SCM service under `LocalService`; credentials are ACL-restricted under `%ProgramData%\Jarvis\worker` |
| `/opt/openwa/compose.jarvis.pending.yaml` | Validated persistent OpenWA Compose candidate adding only the exact API and handoff networks plus `SSRF_ALLOWED_HOSTS=inbound-receiver`; the SSRF guard remains enabled and running OpenWA remains unchanged until supervised installation |

Preparation must leave `/etc/jarvis/jarvis.toml` absent, all Jarvis containers
and `jarvis-*` networks absent, and OpenWA unchanged and healthy.

Read the full pinned application revision from the installed artifact lock and
compare it with the wizard's `LOCKED_REVISION`. Do not substitute the bundle
commit or a mutable branch head.

## Phase 1: verify the unactivated release

Set only non-secret path variables in the administrative shell:

```bash
JARVIS_RELEASE=/opt/jarvis/current
JARVIS_CONFIG=/etc/jarvis/jarvis.toml
JARVIS_ACTIVATION_OVERRIDE=/etc/jarvis/activation.compose.yaml
JARVIS_IMAGE_DIGESTS=/etc/jarvis/image-digests.json
```

Record, without abbreviating, the release commit and relevant hashes:

```bash
docker version
docker compose version
git -C "$JARVIS_RELEASE" status --short
git -C "$JARVIS_RELEASE" rev-parse HEAD
sha256sum "$JARVIS_RELEASE/deployment/artifacts.lock.json"
sha256sum "$JARVIS_RELEASE/deployment/compose.yaml"
```

The status output must be empty. Verify the release using its installed Python
environment and parse the base Compose file without activating its profile:

```bash
PYTHONPATH="$JARVIS_RELEASE/src" \
  "$JARVIS_RELEASE/.venv/bin/python" -m jarvis_control_plane.deployment \
  "$JARVIS_RELEASE/deployment"
docker compose --file "$JARVIS_RELEASE/deployment/compose.yaml" config --quiet
```

Expected verifier summary for this bundle: 13 services, 1056 MiB memory, 1.80
CPU cores, 512 PIDs, and `activation unchanged`.

Do not pass `--profile manual-activation` and do not run `up` in this phase.

### Configuration review

Create `/etc/jarvis/jarvis.toml` from the reviewed schema in
`deployment/config.example.toml`, set `configuration_kind = "active"`, and
replace every example value. Review the exact values independently, then enforce:

```bash
chown root:root "$JARVIS_CONFIG"
chmod 0444 "$JARVIS_CONFIG"
```

The review must confirm the canonical operator, OpenWA internal session, OpenWA
named session, operator conversation, Google subject, OAuth callback, vault,
Ubuntu worker, and Windows worker identities. Record only approved labels and
pass/fail results, not personal identifiers.

Validate the bundle against the active configuration without starting services:

```bash
PYTHONPATH="$JARVIS_RELEASE/src" \
  "$JARVIS_RELEASE/.venv/bin/python" -m jarvis_control_plane.deployment \
  "$JARVIS_RELEASE/deployment" --configuration "$JARVIS_CONFIG"
```

## Phase 2: verify credentials, protocol keys, and storage

Provision credentials only through the administrative credential path documented
in `deployment/README.md`. Required credential boundaries are:

| Boundary | Host path | Expected owner |
| --- | --- | --- |
| Inbound receiver | `/etc/jarvis/credentials/openwa-inbound` | service UID 10001 |
| Capability broker | `/etc/jarvis/credentials/broker` | service UID 10002 |
| Orchestration/OpenAI | `/etc/jarvis/credentials/openai` | service UID 10003 |
| Google | `/etc/jarvis/credentials/google` | service UID 10005 |
| Vault | `/etc/jarvis/credentials/vault` | service UID 10006 |
| OpenWA outbound | `/etc/jarvis/credentials/openwa` | service UID 10007 |
| Windows worker/gateway | `/etc/jarvis/credentials/windows-worker` | service UID 10008 |

Credential directories use mode `0700`; credential files use `0600`. The Google
directory is writable only by UID 10005 because it owns OAuth state and its
refresh token. All other mounts stay read-only to their consuming service.

Every file in `/etc/jarvis/protocol` must be root-owned, group `20000`, mode
`0440`, contain at least 32 random bytes, and be readable only by the exact two
roles named in `<client-role>--<server-role>.key`. Inspect metadata and byte
counts without printing file contents:

```bash
find /etc/jarvis/credentials -xdev -printf '%M %U %G %p\n'
find /etc/jarvis/protocol -xdev -type f -printf '%M %U %G %s %p\n'
```

The reviewer compares the paths and metadata with the Compose mounts. Evidence
records only the number of conforming files and the pass/fail outcome.

### Native workers prepared before the window

The Ubuntu service runs as the dedicated system account `jarvis-worker` with
numeric UID `10008`. Its worker-owned mode-`0400` config is
`/etc/jarvis/native/ubuntu-worker.json`; its listener is
`/run/jarvis-worker/ubuntu.sock`, owned by UID `10008` with mode `0600`. The
gateway container also runs as UID `10008`, so Linux `SO_PEERCRED`, socket owner,
mode, canonical path, and the closed connection ID all have to match before an
action can execute. The service uses a lingering per-user systemd manager only
to create bounded transient action scopes. Preparation enables neither the unit
nor its listener.

The Windows worker is the `JarvisWindowsWorker` SCM service under
`NT AUTHORITY\LocalService`, with startup type Manual. Its private key, client
certificate, CA, closed identity config, immutable wheel, and virtual
environment live below `%ProgramData%\Jarvis\worker` with inheritance removed
and access limited to SYSTEM, Administrators, and LocalService. It opens an
outbound TLS 1.3 connection to `100.106.206.88:9443` using server name
`sami-lenovo.tailb09c76.ts.net`; Windows has no new inbound listener. Preparation
does not start the service.

Create and review these persistent boundaries before activation:

| Path | Owner | Mode |
| --- | --- | --- |
| `/var/lib/jarvis/state` | UID 10002 | `0700` |
| `/var/lib/jarvis/traces` | UID 10002 | `0700` |
| `/var/lib/jarvis/codex-traces` | UID 10003 | `0700` |
| `/var/lib/jarvis/audit` | UID 10004 | `0700` |
| `/var/lib/jarvis/google-traces` | UID 10005 | `0700` |
| `/var/lib/jarvis/vault` | UID 10006 | `0700` |
| `/var/lib/jarvis/deleted-conversations` | UID 10010 | `0700` |
| `/run/jarvis/deleted-archive-ipc` | UID 10010, GID 20000 | `0770` |

Also confirm `/srv/jarvis-workspace` is the single reviewed, read-only Codex
workspace and contains no activation credential.

## Phase 3: prepare workers, backups, and recovery

### Native workers

Install only the separately reviewed service definitions. Before Jarvis starts:

- create `/run/jarvis-worker` with its reviewed owner and mode;
- start the native Ubuntu worker and require
  `/run/jarvis-worker/ubuntu.sock` to be a mode-`0600` Unix socket;
- compare its owner and peer UID with the reviewed worker-gateway credential;
- install the Windows worker with the reviewed TLS 1.3 mTLS material and exact
  application/certificate identity;
- confirm the worker gateway will bind only to its one non-wildcard private
  overlay address and port `9443`;
- confirm no worker listener or Docker socket is exposed publicly.

Do not substitute a different worker identity or execution host. Full worker and
terminal behavior acceptance belongs to ticket 32.

### Backups

Install the locked Python dependencies and the reviewed backup units as described
in `deployment/README.md`. Record the exact active image references in the
root-owned, mode-`0600` `/etc/jarvis/image-digests.json` before relying on the
backup timer.

If a Jarvis release is already active, create and verify a pre-change Jarvis
backup immediately before activation using that active release, its Compose
manifest, and its activated image map:

```bash
PYTHONPATH="$JARVIS_RELEASE/src" \
  "$JARVIS_RELEASE/.venv/bin/python" \
  -m jarvis_control_plane.administrative_backup create \
  --kind pre-change \
  --artifact-lock "$JARVIS_RELEASE/deployment/artifacts.lock.json" \
  --compose-manifest "$JARVIS_RELEASE/deployment/compose.yaml" \
  --image-digests "$JARVIS_IMAGE_DIGESTS"
```

The backup command requires a complete active Compose service set and verifies
its running image IDs. On a true first activation with no active Jarvis release,
do not fabricate that evidence: record that there is no prior Jarvis state,
verify the fresh persistent directories are empty, and create the first baseline
backup after activation and image-map verification.

Separately create a protected, database-consistent backup of the complete
`openwa-data` volume. Treat it as secret material. Do not put it in this
repository, the Jarvis backup, or a Jarvis-readable path.

## Phase 4: prepare the private network topology

There are two distinct OpenWA routes:

1. `jarvis-openwa-api` permits only the Jarvis OpenWA outbound connector to call
   the independently operated OpenWA API.
2. `jarvis-openwa-handoff` permits only OpenWA to deliver signed inbound events
   to `inbound_receiver` through its `inbound-receiver` network alias on `http://inbound-receiver:9011/webhook`.

Do not collapse these routes into a general shared network.

Create both OpenWA networks and the separately reviewed `jarvis-worker-overlay`
as explicitly named Docker networks. They must not publish ports. Record their
pre-attachment IDs and membership before continuing:

```bash
docker network inspect jarvis-openwa-api
docker network inspect jarvis-openwa-handoff
docker network inspect jarvis-worker-overlay
```

If a network does not exist, create it once during the reviewed network-change
step, then inspect its driver, scope, labels, and membership. Do not use a
swarm/overlay network unless that topology was separately reviewed.

### Jarvis activation override

Install an independently reviewed copy of
`deployment/activation.compose.example.yaml` as a root-owned, mode-`0444` file
at `/etc/jarvis/activation.compose.yaml`. Its reviewed content is:

```yaml
services:
  inbound_receiver:
    networks:
      ingress_broker: {}
      openwa_handoff:
        aliases: [inbound-receiver]

networks:
  openwa_handoff:
    external: true
    name: jarvis-openwa-handoff
```

This override is activation state and must remain outside the immutable release
bundle. Render the combined Jarvis model and confirm:

- `inbound_receiver` has exactly `ingress_broker` and `openwa_handoff`;
- `openwa_outbound_connector` has exactly `broker_openwa_outbound` and
  `openwa_api`;
- only `public_oauth_callback` publishes a Jarvis host port, exactly
  `127.0.0.1:8080:8080`;
- `worker_gateway` exposes but does not publish `9443`;
- `deleted_conversation_archive` remains networkless.

For this host, the separately reviewed TLS route is Tailscale Funnel at exactly
`https://sami-lenovo.tailb09c76.ts.net/callback`, forwarding only that path to
`http://127.0.0.1:8080`. Activate it only after the callback container is healthy:

```bash
tailscale funnel --bg --yes --https=443 --set-path=/callback http://127.0.0.1:8080/callback
tailscale funnel status
```

Tailscale strips the public mount prefix before proxying. The target therefore
retains `/callback` so the loopback handler receives its exact closed route;
targeting only port `8080` would forward the request as `/` and fail closed.

Rollback resets the Funnel route before stopping the new Compose release.

```bash
docker compose \
  --file "$JARVIS_RELEASE/deployment/compose.yaml" \
  --file "$JARVIS_ACTIVATION_OVERRIDE" \
  --profile manual-activation config --quiet
```

### OpenWA persistent Compose change

The OpenWA administrator must update its root-owned Compose configuration so the
single `openwa-api` service joins `jarvis-openwa-handoff` while retaining its
existing API route and all established settings. The rendered configuration must
still show:

- the exact pinned OpenWA image digest;
- `ENGINE_TYPE=baileys`;
- the single `openwa-data` volume mounted at `/app/data`;
- automatic session startup;
- `restart: unless-stopped`;
- `stop_grace_period: 45s`;
- port 2785 bound only to the exact private LAN address;
- no public bind and no new published port.
- the SSRF guard remains enabled and `SSRF_ALLOWED_HOSTS` contains exactly
  `inbound-receiver`, allowing the reviewed internal webhook hostname without a
  general SSRF bypass.

Validate the changed OpenWA Compose model before recreating anything:

```bash
sudo docker compose -f /opt/openwa/compose.yaml config --quiet
```

The independent reviewer must compare the complete before/after rendered models.
Only the reviewed network membership and inbound webhook configuration may
change.

## Phase 5: deliberate Jarvis activation

1. Begin the maintenance admission stop and record its timestamp.
2. Confirm both pre-change backups and the rollback release. On a true first
   activation, confirm the recorded no-prior-Jarvis-state condition instead.
3. Re-run the base bundle verifier and combined Compose parse.
4. Start `jarvis-ubuntu-worker.service`, require it to be active, and verify
   `/run/jarvis-worker/ubuntu.sock` is a socket owned by UID `10008` at mode
   `0600`. Start the Windows `JarvisWindowsWorker` service from the Windows host;
   it will reconnect until the gateway listener becomes available.
5. Activate the exact Jarvis profile using both Compose files:

```bash
docker compose \
  --file "$JARVIS_RELEASE/deployment/compose.yaml" \
  --file "$JARVIS_ACTIVATION_OVERRIDE" \
  --profile manual-activation up -d --no-build --pull never
```

6. Require every Jarvis container to be running and healthy. Allow the three
   0.03-CPU egress proxies their reviewed 10-minute cold-start health period; a
   restart or health failure after its applicable start period stops activation.
7. Verify the host-side aggregate status without exposing personal identifiers:

```bash
PYTHONPATH="$JARVIS_RELEASE/src" \
  "$JARVIS_RELEASE/.venv/bin/python" -m jarvis_control_plane.deployment \
  "$JARVIS_RELEASE/deployment" \
  --configuration "$JARVIS_CONFIG" \
  --activation-override "$JARVIS_ACTIVATION_OVERRIDE" \
  --administrative-status --backup-root /var/backups/jarvis
```

8. Inspect actual container image IDs, write the exact service-to-image-ID map to
   `/etc/jarvis/image-digests.json`, make it root-owned and mode `0600`, and
   compare every recorded value with its running container.
9. Verify no service acquired an unexpected mount, capability, port, network, or
   root user.
10. On a true first activation, create the first baseline administrative backup
    now that the complete active service set and verified image map exist.

Do not open message admission until the OpenWA controlled attachment and the
initial health/exposure gates below pass.

## Phase 6: one controlled OpenWA attachment

Immediately before recreation, require OpenWA container `healthy` and the
authenticated named session `ready`. Record only status values and timestamps.

Perform exactly one controlled recreation with the reviewed persistent Compose
change:

```bash
sudo docker compose -f /opt/openwa/compose.yaml up -d --force-recreate openwa-api
```

Then:

- wait independently for container `healthy`;
- query the authenticated sessions endpoint without echoing its API key;
- require the configured named session to return `ready` without a QR;
- require Docker to report a 45-second stop timeout;
- inspect both named networks and require the exact memberships below.

| Network | Exact members |
| --- | --- |
| `jarvis-openwa-handoff` | OpenWA `openwa-api`, Jarvis `inbound_receiver` |
| `jarvis-openwa-api` | OpenWA `openwa-api`, Jarvis `openwa_outbound_connector` |

Container-generated names may differ, so compare container IDs and Compose
labels rather than accepting a textual suffix. Any third member fails the gate.

Confirm the OpenWA host still publishes port 2785 only on its exact private LAN
address and that UFW remains active, default-deny inbound, with only the reviewed
trusted private source. Confirm there is no router forwarding or public bind.

Provision the signed inbound subscriber only after the exact handoff members are
attached:

```bash
sudo -n env PYTHONPATH="$JARVIS_RELEASE/src" \
  "$JARVIS_RELEASE/.venv/bin/python" \
  -m jarvis_control_plane.openwa_webhook \
  --api-base-url http://192.168.1.250:2785/api
```

The command is idempotent. It reads credentials without printing them, updates
or creates exactly one active `message.received` subscriber to
`http://inbound-receiver:9011/webhook`, sets retry count three, and fails closed
if another subscriber competes for the same event. Require its sanitized
`count=1` verification before message admission.

## Phase 7: messaging and persistence acceptance

Use the authenticated OpenWA API procedure in `docs/openwa/operations.md`. Keep
the API key, internal session ID, chat ID, and message bodies only in the remote
shell and never print them.

### Exact inbound

1. The WhatsApp verifier sends one new unique text phrase with a numeric prefix.
2. Resolve the internal session ID from the authenticated sessions response. Do
   not use the human-readable session name in the history URL.
3. Query `GET /api/sessions/:sessionId/messages` and match exactly one inbound
   row with the unique body.
4. Confirm the signed OpenWA webhook reaches Jarvis once and produces exactly one
   admitted request with required audit evidence.
5. Record only direction, count, disposition, request ID, and pass/fail.

### Exact outbound

1. Allow the admitted request to produce one conspicuously labeled text reply.
2. Confirm OpenWA accepts exactly one outbound operation.
3. Ask the verifier to confirm one physical receipt on the dedicated phone.
4. Record the OpenWA status accurately. `sent` plus physical receipt must not be
   misreported as a database `delivered` receipt.
5. Do not retry if acceptance is uncertain; record the outcome as unknown and
   stop the gate.

### Persistence and stop grace

After the exact inbound/outbound checks, perform no more than the one additional
controlled recreation required to prove retained pairing, unless the recreation
in phase 6 is explicitly designated as the persistence proof after a successful
pre-recreation message sample. Require:

- direct return to container `healthy` and session `ready`;
- no QR and no `LOGOUT`;
- the established exact inbound and outbound records still present;
- `stop_grace_period: 45s` still applied by Docker;
- unchanged private bind, UFW policy, restart policy, volumes, and engine.

Avoid extra recreation merely to improve evidence. The acceptance owner must
declare in advance which recreation proves network attachment and persistence.

## Phase 8: exposure, audit, resource, and recovery checks

### Exposure

- [ ] Only the TLS reverse proxy exposes the exact public OAuth `/callback`.
- [ ] Jarvis callback itself binds only `127.0.0.1:8080`.
- [ ] OpenWA remains limited to its exact private LAN bind and reviewed UFW rule.
- [ ] The inbound handoff publishes no port and has exactly two members.
- [ ] Broker, audit, connectors, workers, administrative status, and shell are
  unavailable from LAN and the public Internet.
- [ ] Egress proxies admit only their configured host/port allowlists.

### Audit and secrets

- [ ] Required admission, request, outbound-attempt, and outcome events exist in
  the redacted append-only audit view.
- [ ] No secret or personal identifier appears in Git, images, rendered Compose,
  ordinary state, service logs, or the evidence record.
- [ ] Diagnostic traces remain confined to their manual-administration boundary.
- [ ] An audit append failure blocks all WhatsApp replies; do not induce this
  failure against a live request merely to satisfy ticket 30.

### Settled resources

Wait for synchronization and message processing to settle. Take at least three
samples at fixed, recorded intervals and record:

| Sample | Jarvis memory | Jarvis CPU | Jarvis PIDs | OpenWA memory | OpenWA CPU | Host available memory | Free swap | Free disk |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | | | | | | | | |
| 2 | | | | | | | | |
| 3 | | | | | | | | |

Jarvis must remain within 1056 MiB, 1.80 CPU cores, and 512 PIDs for the reviewed
Compose set, with at least 2 GiB free disk. OpenWA should settle near its
established Baileys envelope; investigate material regression instead of treating
the historical sample as a hard quota.

### Recovery posture

- [ ] Docker and both Compose projects retain `restart: unless-stopped`.
- [ ] The activated image map contains the exact running Jarvis images.
- [ ] The nightly backup timer is installed, enabled, and has a successful next
  and previous run state.
- [ ] No activation command pulls, migrates, upgrades, or replaces the release on
  reboot.
- [ ] Full-host reboot acceptance is scheduled or recorded separately; do not
  claim it from a controlled container recreation.

## Rollback

Rollback is mandatory when a stop condition occurs or an acceptance gate cannot
establish a safe result.

1. Stop new Jarvis admission.
2. Preserve sanitized timestamps, status codes, request IDs, image IDs, network
   membership, and the failed gate. Do not copy secret logs into evidence.
3. Do not replay interrupted or outcome-unknown work.
4. Stop the new Jarvis Compose release using the same base and activation files:

```bash
docker compose \
  --file "$JARVIS_RELEASE/deployment/compose.yaml" \
  --file "$JARVIS_ACTIVATION_OVERRIDE" \
  --profile manual-activation down
```

5. If the handoff change caused the failure and OpenWA has not reported
   `LOGOUT`, restore the previously reviewed OpenWA Compose configuration and
   perform at most one controlled recreation. Require `healthy` and `ready`.
6. If WhatsApp reported `LOGOUT`, do not recreate or re-pair. Preserve state and
   investigate outside the acceptance window.
7. Restore the compatible pre-change Jarvis state through the ticket 28 manual
   restore procedure, activate the previous pinned Jarvis release, and repeat its
   health and smoke checks.
8. Verify OpenWA's original bind, firewall, volume, engine, stop grace, and
   session state. Confirm no unexpected network member remains.
9. Leave ticket 30 open and record the failed row as a blocker. Retest only after
   the cause is understood and the runbook or contract is deliberately updated.

`down` must never include `--volumes`. Never remove `openwa-data`, credential
directories, pairing state, backups, audit data, or diagnostic traces as part of
rollback.

## Acceptance evidence worksheet

Complete every row with a timestamp, operator/reviewer initials, sanitized
evidence pointer, and outcome.

| Gate | Required evidence | Outcome |
| --- | --- | --- |
| Release pin | Installed commit, artifact-lock hash, locked application revision/source hash | |
| Offline validation | Bundle verifier summary and base Compose parse | |
| Active configuration | Independent identity/schema review; root ownership and `0444` mode | |
| Credentials | Exact path/owner/mode review with no values displayed | |
| Protocol keys | Exact pair mounts, root:GID 20000, `0440`, minimum byte count | |
| Persistent paths | Exact owners/modes and non-Jarvis-readable backup root | |
| Native services | Reviewed Ubuntu/Windows definitions and identity registration | |
| Backups | Verified Jarvis pre-change snapshot and protected OpenWA volume backup | |
| Jarvis activation | Exact Compose files/profile, all containers healthy, exact image map | |
| Handoff topology | Exactly two members, no published port | |
| Outbound API topology | Exactly OpenWA and outbound connector | |
| Exposure | OAuth-only public route; unchanged OpenWA LAN bind/UFW; no private surface | |
| OpenWA readiness | Container `healthy` plus named session `ready` | |
| Exact inbound | One unique text, internal session ID route, one admitted request | |
| Exact outbound | One accepted send and one physically confirmed receipt | |
| Persistence | Controlled recreation returns without QR to `healthy` and `ready` | |
| Stop grace | Docker reports 45 seconds | |
| Audit | Required redacted evidence exists and no audit gate failed | |
| Secret review | No secret/personal identifier in prohibited locations | |
| Resources | Three settled samples within reviewed bounds | |
| Recovery | Restart policies, image map, backup timer, rollback readiness | |
| Stop conditions | Explicit confirmation that none occurred | |

## Closing ticket 30

Only after every worksheet row passes:

1. Check all three acceptance boxes in ticket 30.
2. Append a `## Comments` entry with the sanitized activation date, exact pinned
   release, evidence-record pointer, OpenWA health/readiness results, topology,
   inbound/outbound counts, persistence result, stop grace, exposure result, and
   resource summary.
3. Set `Status: complete`. Do not use `ready-for-agent` or `ready-for-human` as a
   completion status.
4. Keep tickets 31 and 32 open. Real Google/vault behavior and complete
   Ubuntu/Windows/terminal/Codex behavior have their own supervised acceptance.
