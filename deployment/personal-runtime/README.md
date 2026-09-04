# Native personal runtime deployment

The supported deployment is one native Ubuntu `systemd` service. OpenWA remains
a separate Docker Compose project and reaches the runtime through one private
Docker host-bridge listener. Do not add the assistant to OpenWA's Compose graph.

## Package verification

From a clean checkout, verify the surviving runtime before packaging:

```console
uv sync --locked
uv run pytest tests/personal_runtime
uv run ruff check src/jarvis_personal_runtime tests/personal_runtime
uv run ruff format --check src/jarvis_personal_runtime tests/personal_runtime
uv run python -m compileall -q src/jarvis_personal_runtime
```

On Ubuntu, stage each reviewed Git revision in its own commit-named directory
under `/opt/jarvis-personal-runtime/releases/`. Verify the replacement checksum
manifest, build from the hash-locked requirements, and install the local package
without resolving additional dependencies:

```console
cd /opt/jarvis-personal-runtime/releases/COMMIT
sha256sum --check deployment/personal-runtime/SHA256SUMS
uv venv --python /usr/bin/python3.13 .venv
uv pip install --python .venv/bin/python --require-hashes \
  -r deployment/personal-runtime/requirements.lock
uv pip install --python .venv/bin/python --no-deps --no-build-isolation .
```

Do not place credentials in the release directory. Do not change the active
`current` symlink until the candidate, configuration, and rendered unit pass
inactive validation.

## Runtime root and configuration

The service uses `/var/lib/jarvis-personal-runtime` as its private runtime root
and `/etc/jarvis/jarvis.toml` as its one active TOML configuration. There is no
pending configuration file. Create the files from the supplied examples. They
have distinct trust boundaries:

| File | Contents | Required ownership and mode |
| --- | --- | --- |
| `.env` | OpenAI, OpenWA, and Google API OAuth credentials | `root:jarvis-personal-runtime`, `0440` |
| `/etc/jarvis/jarvis.toml` | Non-secret settings and saved permissions | symlink to service-owned `personal-runtime/jarvis.toml`, `0600` |
| `SYSTEM.md` | Editable system prompt | `jarvis-personal-runtime:jarvis-personal-runtime`, `0600` |

Jarvis never edits `.env` or `SYSTEM.md`; it writes only the
`[saved_permissions]` section of `/etc/jarvis/jarvis.toml`. Runtime-owned cache
and trace paths must stay below the runtime root. An optional vault path may be
an absolute read-only directory outside it.

Keep `/etc/jarvis` root-owned because it may contain unrelated protected files.
Install the configuration in a dedicated service-owned subdirectory and expose
the one operator-facing path with a stable symlink. The loader resolves the
symlink before Jarvis atomically updates saved permissions:

```console
sudo install -d -o root -g root -m 0755 /etc/jarvis
sudo install -d -o jarvis-personal-runtime -g jarvis-personal-runtime -m 0700 \
  /etc/jarvis/personal-runtime
sudo install -o jarvis-personal-runtime -g jarvis-personal-runtime -m 0600 \
  deployment/personal-runtime/jarvis.toml.example \
  /etc/jarvis/personal-runtime/jarvis.toml
sudo ln -sfn personal-runtime/jarvis.toml /etc/jarvis/jarvis.toml
sudo -u jarvis-personal-runtime nano /etc/jarvis/jarvis.toml
```

Always edit that file directly. Do not create `jarvis.toml.pending` or another
working copy, and do not edit the symlink target by its internal path. Stop the
service before changing trust-critical identities, listener values, paths, or
command permissions, validate the edited file, and restart only after
validation succeeds.

Required `.env` names are `OPENAI_API_KEY`, `OPENWA_API_KEY`, and
`OPENWA_WEBHOOK_SIGNING_SECRET`. The active personal Google route also requires
`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and
`GOOGLE_OAUTH_REFRESH_TOKEN`; never print or copy these credentials into TOML.
Set the exact authorized account in non-secret TOML:

```toml
[google]
account = "kayyal.sami@gmail.com"
max_output_chars = 20000
```

The supervised OAuth grant is expected to include `openid` and email identity,
`gmail.readonly`, `gmail.send`, `drive.readonly`, and `calendar.events`.
Credentials remain only in `.env`; OAuth tokens and authorization headers must
never enter model context or runtime traces. Copy the three reviewed JSON
manifests from `deployment/personal-runtime/manifests/` to the runtime root's
private `manifests/` directory only when a separate generic MCP service is
configured. The `[google]` section is the active personal route; do not model it
as a `[[mcp_services]]` entry. Required production TOML values include the
private listener, OpenWA API base URL, internal session ID, named session,
authorized operator number and chat ID, Ubuntu working directory, and read-only
prefixes. Configure all four
Windows SSH fields together when Windows execution is enabled. The identity file
must be private to the service account and the SSH host key must already be
pinned in that account's known-hosts file.

Validate without binding a socket or contacting providers:

```console
sudo -u jarvis-personal-runtime \
  /opt/jarvis-personal-runtime/releases/COMMIT/.venv/bin/jarvis-personal-runtime \
  --root /var/lib/jarvis-personal-runtime \
  --config /etc/jarvis/jarvis.toml --check
```

Never print `.env`, private keys, phone/chat identifiers, raw webhook payloads,
or trace bodies during validation.

`--check` parses and validates the Google configuration and every configured
generic MCP manifest without contacting Google. Normal startup validates the
Google API configuration; separately configured MCP services additionally
perform live discovery and fail closed if a selected operation digest, protocol
version, or server identity has changed. `/connect google` refreshes the
configured OAuth grant and binds one in-memory Google connection;
`/connections` reports its state and `/disconnect google` invalidates it.
Connection replacement or disconnection also invalidates pending Google writes.
Google writes accept only `1`, `9`, or `/cancel`, are attempted once, and are
never automatically retried after an ambiguous outcome.

The active personal route uses Google's generally available official Gmail,
Drive, and Calendar REST APIs. It exposes bounded Gmail search/read and
send/reply, Drive search/metadata/text content/export only, and Calendar
search/read plus create/update. Gmail and Calendar writes require exact
one-attempt approval. Drive has no mutation or destructive operation, and no
tool may select an arbitrary Google endpoint. The hosted Workspace Developer
Preview MCP route is not used: personal Gmail cannot enroll in that program,
and the hosted Gmail MCP has no send/reply operation. The generic configured MCP
service capability remains available for separately configured services.

Activate this route only under human supervision: verify the exact account and
OAuth consent, validate the private configuration, prove restart persistence,
run bounded reads, perform the approved real Gmail and Calendar acceptance
fixtures, verify rejection and excluded mutations, test disconnect/reconnect,
and confirm the unchanged WhatsApp, vault, and terminal paths before the human
operator makes the final go/no-go decision.

## Install or update the service

Render the four `@...@` placeholders in
`jarvis-personal-runtime.service` with the reviewed service user, group, release
root, and runtime root. Reject whitespace, `%`, `|`, and shell metacharacters in
rendered values. Review and verify the complete result before installation:

```console
systemd-analyze verify /path/to/rendered-jarvis-personal-runtime.service
sudo install -o root -g root -m 0644 \
  /path/to/rendered-jarvis-personal-runtime.service \
  /etc/systemd/system/jarvis-personal-runtime.service
sudo systemctl daemon-reload
```

For an update, validate the candidate against the live runtime root while the
current service continues running. Then stop the service, atomically replace
`/opt/jarvis-personal-runtime/current`, start it, and verify the exact target:

```console
sudo systemctl stop jarvis-personal-runtime
sudo ln -sfn /opt/jarvis-personal-runtime/releases/COMMIT \
  /opt/jarvis-personal-runtime/current.new
sudo mv -T /opt/jarvis-personal-runtime/current.new \
  /opt/jarvis-personal-runtime/current
sudo systemctl start jarvis-personal-runtime
sudo systemctl is-active jarvis-personal-runtime
sudo systemctl is-enabled jarvis-personal-runtime
sudo readlink -f /opt/jarvis-personal-runtime/current
```

Keep the prior commit-named replacement release until post-update WhatsApp and
command checks pass. Roll back only by stopping the service, atomically restoring
that prior replacement symlink, starting the service, and repeating every health
gate. There is no legacy control-plane fallback.

## Private OpenWA handoff

Set `listener_host` to the exact RFC1918 gateway of OpenWA's Docker bridge and
use a reviewed unprivileged port. The runtime rejects wildcard, loopback, public,
hostname, and IPv6 listener values. OpenWA's only `message.received` webhook must
target `http://BRIDGE_GATEWAY:PORT/webhook`.

Before changing OpenWA or the firewall, record container identity, image digest,
volume, networks, health, named-session readiness, webhook destination, bridge
interface/gateway, and current OpenWA container address. Any OpenWA recreation or
firewall change requires separate operator approval. Preserve `openwa-data`,
Baileys pairing, the pinned image, and the existing private LAN exposure.

If the host firewall defaults to deny, admit only the current OpenWA container
address on the exact bridge interface:

```console
sudo ufw allow in on BRIDGE_INTERFACE from OPENWA_CONTAINER_IP \
  to BRIDGE_GATEWAY port PORT proto tcp \
  comment 'Jarvis personal runtime from OpenWA only'
```

Never allow the whole bridge subnet, another interface, LAN/Tailscale peers, or
`Anywhere`. After any OpenWA container recreation, rediscover its address and
revalidate the source-specific rule before sending traffic.

## Operation and health

```console
sudo systemctl status jarvis-personal-runtime --no-pager
sudo journalctl -u jarvis-personal-runtime --since today --no-pager
sudo ss -ltnp
sudo docker compose -f /opt/openwa/compose.yaml ps
```

A healthy assistant requires all of the following:

1. `jarvis-personal-runtime` is enabled and active with no restart loop.
2. Its listener is bound only to the configured bridge gateway and port.
3. OpenWA is `healthy` and the exact configured named session is `ready`; API
   routes use the distinct configured internal session ID where required.
4. No QR, `LOGOUT`, pairing change, container/volume replacement, or exposure
   broadening occurred.
5. A real authorized message produces one expected phone reply, and traces show
   one admitted text, its expected deterministic-command handling or ordinary
   request, and one outbound attempt per chunk.

`/api/health/ready` alone does not prove WhatsApp readiness. Query the
authenticated sessions endpoint, distinguish each named session from its
internal session ID, and never echo the API key. Treat `LOGOUT`, a
fresh QR, identity mismatch, pairing loss, or ambiguous delivery as a hard stop;
preserve evidence and do not repeatedly recreate or re-pair OpenWA.

The runtime trace is verbatim and may contain message text, tool payloads,
terminal output, and credentials supplied in conversation. Keep the runtime root
private and exclude it from ordinary source-control and log collection.
