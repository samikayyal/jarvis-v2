Type: grilling
Status: resolved
Blocked by: 10

## Question

What deployment topology, service lifecycle, configuration and secret layout, observability, backup and recovery rules, failure messages, resource limits, upgrade procedure, and end-to-end acceptance matrix make the V1 specification implementation-ready without weakening the verified OpenWA messaging gateway?

## Answer

V1 uses a small hybrid deployment on the Ubuntu control host. The Jarvis control
plane is one independently operated Docker Compose project at
`/opt/jarvis/compose.yaml`; the verified OpenWA gateway remains a separate
Compose project at `/opt/openwa/compose.yaml`. The Ubuntu execution worker is a
minimal native systemd service because a container cannot execute against the
host without unsafe privileged access, broad host mounts, or the Docker socket.
The Windows execution worker is likewise a native Windows service.

This is an operational contract, not authorization for deployment. Images,
configuration, credentials, service definitions, firewall rules, worker
registration, and activated releases remain trust-critical Jarvis components.
Jarvis may prepare and test changes, but a human administrator must activate
them.

### Deployment topology

The Compose project name is fixed as `jarvis`. Every container runs as a
non-root user, has a read-only root filesystem except for its explicit state or
temporary mount, drops all Linux capabilities unless one is documented as
required, uses `no-new-privileges`, and receives only its own configuration and
credential mounts. No Jarvis container receives the Docker socket, broad host
filesystem access, host PID namespace, or privileged mode.

| Component | Deployment and connectivity | Durable writes |
| --- | --- | --- |
| Inbound receiver | Dedicated container on the private OpenWA handoff network and the broker-local network | Atomic inbox admission through the broker only |
| Capability broker and state owner | Dedicated container; sole hub for lifecycle, policy, approvals, permissions, outbox, and dispatch | Jarvis SQLite state volume |
| Orchestration agent | Dedicated container on broker-local egress-controlled network | No authoritative state writes |
| Audit service | Dedicated container reachable only from the broker and bounded administrative status path | Append-only audit volume |
| Google connector | Dedicated container reachable only from the broker; outbound access limited to required Google endpoints | Its private OAuth token directory only |
| Vault connector | Dedicated container reachable only from the broker | Dedicated vault clone and Git metadata only |
| OpenWA outbound connector | Dedicated container on the broker-local and private OpenWA handoff networks | Outbound-attempt state through the broker only |
| Worker gateway | Dedicated container; overlay-only authenticated worker transport | Registered host/certificate metadata through the broker |
| Ubuntu worker | Native systemd service under a dedicated unprivileged identity; local authenticated worker channel | Bounded action workspace and process metadata only |
| Windows worker | Native Windows service under a dedicated identity; outbound private-overlay mTLS session | Bounded action workspace and process metadata only |
| Public OAuth callback | Dedicated narrow container or connector-owned endpoint exposing only the registered HTTPS callback | Single-use state handoff and Google token replacement only |

OpenWA and the inbound receiver share one explicitly named, private,
non-published Docker network. No other Jarvis component joins it. This refines
ticket 10's loopback requirement while preserving its security invariant: the
webhook handoff is local to the host, is unavailable from the LAN and internet,
and connects only OpenWA to the receiver. Adding this network requires one
controlled OpenWA recreation and the complete existing OpenWA re-verification
ladder. It does not combine the Compose projects, images, volumes, lifecycle, or
upgrade procedures.

All other inter-container traffic uses private, non-published networks separated
by trust boundary. Only the exact Google OAuth callback is public. The worker
gateway binds only to the private-overlay interface. Administrative status is
local or private-overlay-only. There is no public Jarvis API, webhook, dashboard,
broker port, audit view, connector port, or shell.

### Lifecycle and readiness

The currently activated Jarvis release starts automatically at host boot with
`restart: unless-stopped`. Automatic recovery may relaunch only that exact
release and configuration; it never pulls an image, changes a digest, migrates
state opportunistically, or activates a trust-critical change.

Compose health and dependency ordering do not by themselves authorize work.
The broker admits a new request only when:

- its state store is writable and consistent;
- the audit service can append a required event;
- the inbound receiver and broker agree on the configured OpenWA session and
  operator identity;
- OpenWA is both container `healthy` and named session `ready`;
- the required connector for the request reports ready; and
- the selected execution worker, when any, has the expected registered identity.

A connector or worker may be unavailable without making safe unrelated reads
unavailable. Degraded state is explicit in `/status` and `jarvisctl status`.
There is no host failover. A restart marks active work interrupted, invalidates
pending actions, revokes session permissions, and never resumes a model run,
connector call, Codex task, terminal process, or ambiguous external side effect.

Shutdown first stops admission, acknowledges already durably claimed inbound
messages, cancels or interrupts current work within a bounded grace period,
invalidates pending actions, flushes state and audit writes, and then stops
containers. OpenWA is not stopped when the Jarvis project stops.

### Configuration and credential layout

All non-secret runtime configuration lives in one root-owned, read-only
`/etc/jarvis/jarvis.toml`. It includes fixed service identities, model policy,
operator and OpenWA references in their safe canonical form, network and timeout
bounds, resource limits, connector operation allowlists, paths, and retention
settings. Startup performs complete schema and cross-field validation before a
component can become ready. Unknown keys, missing required values, invalid
permissions, floating image references, or inconsistent identities fail closed.

Credential-class material lives outside the repository, Compose manifest,
ordinary state, environment variables, command lines, image layers, logs, and
routine backups under `/etc/jarvis/credentials/<service>/`. Static credential
files are root-owned mode `0600`. A connector that must rotate its credential
owns only its own `0700` directory and `0600` file. Each directory is mounted
read-only into only its consuming container except for the Google connector's
explicit token-replacement path. Docker secrets do not replace this established
service-specific plaintext-file boundary.

Configuration and credential changes are manual trust-critical activations.
Jarvis cannot edit or reload its own active policy, identity, credential, network,
or service configuration.

### Observability

V1 uses bounded local observability and adds no Prometheus, Grafana, Loki, cloud
logging, or external monitoring service:

- every container and native worker has a narrowly scoped health check;
- `jarvisctl status` reports component readiness, OpenWA container health,
  WhatsApp session readiness, audit writability, backup freshness, Ubuntu worker
  state, Windows worker availability, current release digest, and resource/disk
  pressure without revealing secrets or personal identifiers;
- `/status` exposes only the already defined safe operator-facing session and
  request state;
- sanitized structured container logs use Docker rotation capped at five
  20-MiB files per container;
- complete diagnostic traces retain every captured payload, including
  credentials, indefinitely under a manual-administration-only boundary and are
  never automatically redacted or deleted; and
- the permanent redacted audit remains the security record and is not inferred
  from diagnostic logs.

V1 sends no unsolicited WhatsApp health alerts because it is completely
reactive. A failure is visible in the current interaction, deterministic status
commands, and local administrative checks.

### Backups and recovery

An administrative job creates a versioned backup nightly and immediately before
every manual upgrade, active-configuration change, or state migration. Backups
live under the root-only `/var/backups/jarvis/` boundary outside Jarvis-readable
paths and are retained indefinitely without automatic deletion.

The backup uses SQLite's online backup mechanism and a consistent snapshot of
the append-only audit store, deleted-conversation archive, and complete
diagnostic-trace store. It also includes the active non-secret configuration,
Compose manifest, exact image digests, release metadata, and schema version.
Credential files, private keys, OpenWA pairing records and data, ephemeral
working data not already captured in traces, caches, and every external
service's authoritative content remain excluded. Because full traces are
unredacted, backups may contain credential-class secrets and use the same
highest-sensitivity manual-administration boundary. OpenWA retains its existing
separate protected volume-backup procedure.

`jarvisctl status` reports backup age, last result, and free space. A stale or
failed backup is a visible degraded condition but does not alone authorize
deletion of older backups. Disk pressure follows the resource rules below.

Recovery is always manual: restore into an isolated path, validate checksums,
ownership, permissions, SQLite integrity, audit readability, schema version,
and release compatibility, then stop Jarvis and deliberately activate the
validated restore. Credentials are reprovisioned separately. A restore rehearsal
is mandatory before initial acceptance and after every storage-schema change.

### Failure and retry contract

Every operator-facing failure states, in this order:

1. what failed in plain language;
2. whether an external change definitely did not occur, definitely occurred, or
   may have occurred;
3. what Jarvis did next; and
4. the exact safe next step for the operator.

It ends with a stable bounded error code and request ID. It never includes raw
stack traces, secrets, personal identifiers, private credential paths, unbounded
commands or output, connected-service content, or internal model reasoning.

A provably read-only operation may receive a bounded retry inside the same
active request. Validation, authentication, authorization, policy, resource,
and deterministic rejection failures are not retried automatically. Gmail,
Calendar, Git push, OpenWA send/reply, and terminal outcomes that may have
succeeded are recorded as unknown, never retried automatically, and require
manual reconciliation or a new explicit instruction. Host unavailability never
causes failover.

### Resource envelope

The 3.7-GiB, four-core Ubuntu host supports a lightweight cloud-backed assistant,
not local inference or a new infrastructure tier. The Jarvis Compose project has
a combined hard ceiling of 1.25 GiB RAM and two CPU cores, enforced through
explicit per-container limits whose sum does not exceed that envelope. Every
container also has a bounded PID count. Native worker processes use bounded
process scopes, deadlines, and output limits and are included in operational
resource observation.

V1 runs no local LLM, embedding model, Redis, PostgreSQL, queue service, object
store, metrics stack, or other resident infrastructure. It retains one active
request, one pending action, and one terminal action. Captured terminal stdout
and stderr are each capped at 1 MiB; excess is discarded with a visible
truncation marker. Connector result counts, content bytes, model context, agent
turns, execution time, and temporary disk use follow the conservative defaults
and hard maxima in the implementation specification; startup rejects any value
above those maxima.

Disk status warns below 20 GiB free or 10 percent free, whichever threshold is
crossed first. Below 2 GiB free or when the next full-trace reservation cannot be
guaranteed, Jarvis starts no trace-producing work and never deletes retained
traces automatically. Whenever the audit service cannot append, every WhatsApp
message and side effect is blocked while safe reads remain local-
administration-only. Sustained swapping, an OOM kill, or a repeated crash loop
makes the affected component unhealthy; Docker restart is not presented as
recovery success.

### Manual upgrade and rollback

Upgrades use a planned Jarvis-only maintenance window. They never use floating
tags, unattended image updates, or an OpenWA image/configuration change hidden
inside a Jarvis release.

1. Record the target release, immutable image digests, expected schema change,
   and rollback compatibility.
2. Enter maintenance mode, stop new admission, finish or cancel the active
   request, and invalidate the pending action.
3. Create and validate the required pre-upgrade backup.
4. Pull the pinned images and run Compose/configuration/schema validation
   without activating them.
5. Restore the backup into an isolated path and rehearse any migration there.
6. Stop only the Jarvis project and native Ubuntu worker.
7. Apply the reviewed migration, activate the exact release, and start Jarvis.
8. Require all component, audit, connector, network-exposure, identity, and
   resource health checks.
9. Reconcile only the known maintenance window through bounded OpenWA history,
   applying the durable `(session ID, WhatsApp message ID)` deduplication key;
   history polling never becomes the normal trigger.
10. Run the post-upgrade acceptance smoke matrix before leaving maintenance.

On failure, stop the new release, restore the compatible pre-upgrade state when
needed, reactivate the prior pinned digests and configuration, and repeat health
and smoke checks. No interrupted or outcome-unknown side effect is replayed.

### End-to-end acceptance matrix

Mocks and automated tests are necessary but insufficient. Initial activation
requires both a passing automated suite and a supervised run against the real
dedicated WhatsApp account, configured Google account, private vault repository,
Ubuntu worker, and Windows worker. Test data is conspicuously labeled and every
real side effect is reversible and approved through the production control flow.
Excluded or destructive behavior is verified only by rejection.

| Area | Required acceptance evidence |
| --- | --- |
| Build and configuration | Reproducible pinned images; no floating tags; complete config/schema validation; secrets absent from Git, images, Compose rendering, environment, logs, and ordinary state; exact mount ownership/modes verified. |
| Network exposure | Only the OAuth callback is public; OpenWA-to-ingress network has no published port and only two members; broker, audit, connectors, worker control, status, and shell are unavailable from LAN/internet; overlay and mTLS host identity checks pass. |
| OpenWA preservation | After the one controlled network attachment, require Compose validation, container `healthy`, session `ready`, exact inbound text, confirmed outbound receipt, retained auth after controlled recreation, 45-second stop timeout, unchanged LAN/UFW exposure, and settled resource checks. `LOGOUT` stops testing without repeated recreation or re-pairing. |
| Admission and replay | Verify every disposition and HTTP result for bad signature, malformed envelope, duplicate, wrong event/session, group, status, media, reaction, `fromMe`, blank text, unresolved identity, unauthorized sender, audit outage, and unwritable inbox; duplicate webhook and old approval replays create no duplicate work or side effect. |
| Session and control grammar | Verify `/new`, `/status`, model/config commands, one active request, one pending action, ten-minute expiry, deterministic whole-message approval/rejection, unrelated-text blocking, the exact busy response with no queue or request mutation, cancellation, and permission list/revoke behavior. |
| Agent and prompt-injection containment | Connected content, terminal output, quoted text, model output, and Codex output cannot create approval, policy, permission, identity, connector access, or side effects; malformed proposals fail schema validation. |
| Google reads | Real bounded Gmail, Calendar, and Drive reads succeed with fixed scopes and produce no side effect; wrong account, revoked token, missing scope, rate limit, timeout, and sanitized error behavior are exercised. |
| Gmail and Calendar writes | Send one labeled email and create/update one labeled operator-owned calendar event only after exact approval; altered/expired/replayed approval fails; unknown outcome is not retried; destructive Google and Drive operations are unavailable. |
| Vault | Real deterministic read succeeds; one labeled Markdown diff is approved against an exact base, committed as the configured Jarvis identity, and normally pushed; dirty clone, changed base, conflict, non-fast-forward, excluded path, deletion, force-push, and history rewrite fail closed. |
| Terminal policy | Safe reads run only within deterministic bounds; approval and exact session/persistent permission paths work; cwd/host/argument/path changes invalidate matching; mandatory-fresh classes never create permissions; hard prohibitions and trust-critical activation remain impossible. |
| Ubuntu worker | Exact host-bound action, deadline, output truncation, process-tree cancellation, disconnect, identity mismatch, and no-failover behavior pass without privileged container or Docker-socket access. |
| Windows worker | Outbound mTLS registration, exact host-bound action, Job Object cancellation, offline state, reconnect, identity mismatch, and no-failover behavior pass. |
| State, audit, and traces | Restart interruption, pending-payload removal, session-permission revocation, persistent-permission survival, history/memory lifecycle, working-cache clearing, permanent unredacted full traces including credential payloads, trace-bearing backup restore, redacted append-only audit evidence, and zero WhatsApp delivery on audit failure pass. |
| Failure messages | Representative validation, auth, policy, host, connector, rate-limit, timeout, disk, audit, and outcome-unknown errors follow the four-part safe format with stable code/request ID and no sensitive leakage. |
| Backup and restore | Nightly/pre-change job succeeds; contents and exclusions match policy; isolated restore passes checksums, permissions, SQLite integrity, audit readability, schema compatibility, and credential reprovisioning; restored release passes smoke tests. |
| Resource and endurance | Every implementation-spec default and hard maximum is enforced; the defined two-hour controlled workload and 60-minute supervised workload stay within the 1.25-GiB/two-core envelope, five-second measurements, swap and trace-growth bounds, and ten-minute settling window without OOM, unbounded disk growth, payload loss, or restart loop. |
| Upgrade and rollback | Maintenance admission stop, backup, isolated migration rehearsal, pinned upgrade, known-window deduped reconciliation, smoke tests, forced failed upgrade, and restoration of the prior release/state all pass without touching OpenWA. |
| Full host reboot | Docker and both Compose projects recover; OpenWA returns to `healthy` and `ready` without pairing; only the activated Jarvis release starts; interrupted work does not resume; session permissions are revoked; persistent permissions and state survive; identities/permissions/exposure remain correct; Ubuntu worker returns and Windows state is independent; post-reboot inbound and outbound messaging pass. |

Acceptance fails on any bypass, secret disclosure, duplicate side effect,
unexplained external outcome, unauthorized network exposure, missing audit event,
resource breach, unrecoverable backup, automatic trust-critical activation, or
regression of OpenWA's verified gateway contract. Waivers are not implicit: a
failed row remains an explicit implementation blocker until retested or the V1
contract is deliberately reopened.

## Comments

- Amended on 2026-08-02 to correct the diagnostic-trace synthesis error and
  align operations with the authorized full-payload indefinite trace contract,
  trace-bearing backups, no automatic trace deletion, no WhatsApp delivery while
  audit is unavailable, the explicit ingress/busy behavior, and the conservative
  numerical limits and endurance workloads in the implementation specification.
- During resolution, the Docker choice refined ticket 10's literal host-loopback
  webhook wording to a private, non-published Docker network with only OpenWA and
  the inbound receiver as members. The security invariant remains local-only
  transport with no LAN or public exposure.
- The Ubuntu execution worker is deliberately the only native Ubuntu Jarvis
  service. Containerizing it would require privileged host access and violate
  the locked worker boundary.
