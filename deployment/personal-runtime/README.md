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

The service uses `/var/lib/jarvis-personal-runtime` as its private runtime root.
Create `.env`, `jarvis.toml`, and `SYSTEM.md` there from the supplied examples.
The files have distinct trust boundaries:

| File | Contents | Required ownership and mode |
| --- | --- | --- |
| `.env` | OpenAI and OpenWA credentials | `root:jarvis-personal-runtime`, `0440` |
| `jarvis.toml` | Non-secret settings and saved permissions | `jarvis-personal-runtime:jarvis-personal-runtime`, `0600` |
| `SYSTEM.md` | Editable system prompt | `jarvis-personal-runtime:jarvis-personal-runtime`, `0600` |

Jarvis never edits `.env` or `SYSTEM.md`; it writes only the
`[saved_permissions]` section of `jarvis.toml`. Runtime-owned cache and trace
paths must stay below the runtime root. An optional vault path may be an absolute
read-only directory outside it.

Required `.env` names are `OPENAI_API_KEY`, `OPENWA_API_KEY`, and
`OPENWA_WEBHOOK_SIGNING_SECRET`. Required production TOML values include the
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
  --root /var/lib/jarvis-personal-runtime --check
```

Never print `.env`, private keys, phone/chat identifiers, raw webhook payloads,
or trace bodies during validation.

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
