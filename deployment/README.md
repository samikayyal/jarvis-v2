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

The image entry point is the role-specific runtime, not the offline verifier.
Each Compose service selects exactly one composition root and exposes only its
closed operation set. Owned-service calls use bounded JSON frames authenticated
in both directions with a distinct HMAC key for each client-to-server link.

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

The native Ubuntu and Windows workers are not Compose services. Before activation,
a root administrator creates `/run/jarvis-worker`, starts the reviewed native
Ubuntu worker so its mode-`0600` socket is present there, and verifies ownership
against the configured peer UID. Compose mounts that exact socket read-only and
the worker gateway consumes the reviewed authenticated Ubuntu socket and registers the
outbound Windows transport, but this bundle does not install either worker,
provision identities or certificates, or alter either host. OpenWA is not a
service in this Compose project; only its future handoff is documented in
`openwa-handoff.md`.

The worker gateway listens for the Windows worker only on the exact configured
private-overlay address (port `9443` in the reviewed Compose metadata), requires
TLS 1.3 client authentication and the registered certificate/application
identity, and attaches the worker-initiated session to the gateway transport.
The native Windows service runs `run_windows_worker_client` with its existing
Job Object executor; installation and activation remain manual-only.

The callback process intentionally binds plain HTTP only on host loopback. A
separately reviewed host TLS reverse proxy must terminate the exact configured
HTTPS callback URL and route only `/callback` to `127.0.0.1:8080`; it must not
expose any private service port. That proxy is an activation prerequisite, not
an unreviewed Compose service.

Fresh persistent directories are also a manual activation prerequisite. Create
the state/trace paths for UID 10002, audit for UID 10004, Google trace and
credential state for UID 10005, and the vault clone for UID 10006, all with the
reviewed `0700` directory mode. The long-lived services never start as root and
do not repair ownership themselves.

The image build is reproducible from the Git-pinned application artifact, the
digest-pinned Python base, and the hash-locked exported requirements. Building,
publishing, credential provisioning, worker installation, OpenWA attachment,
and activation are separate manual-administration steps.
