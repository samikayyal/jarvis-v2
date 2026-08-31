# Native personal runtime package

This directory packages the replacement as one native Ubuntu `systemd` service.
It does not install, enable, start, stop, or replace any service by itself. Ticket
12 owns live installation and cutover with the authorized operator present.

The rendered service reads `.env`, `jarvis.toml`, and `SYSTEM.md` directly from
the discovered runtime root. The discovered service account owns that directory. The
static `.env` is root-owned, group-readable only by the consuming service, and
mode `0440`. The service account owns the mode-`0600` TOML and system prompt;
the operator may edit them through an administrative editor. The runtime changes
only `[saved_permissions]` in TOML. The seven-day message-ID cache and
the rotating verbatim JSON Lines trace live below `data/`; full rotated segments
are retained as `runtime-trace.jsonl.N` rather than deleted.

Ordinary process logs and uncaught errors go to `journald`. They are separate
from the verbatim runtime trace.

## Validation without activation

From a clean repository checkout, these checks inspect development copies only:

```console
uv run pytest tests/personal_runtime/test_config.py tests/personal_runtime/test_service.py tests/personal_runtime/test_package.py
uv run ruff check src/jarvis_personal_runtime tests/personal_runtime
uv run ruff format --check src/jarvis_personal_runtime tests/personal_runtime
```

On the target Ubuntu host, first discover and record the exact release root,
runtime root, service user, service group, OpenWA Docker-bridge gateway, listener
port, and private OpenWA API URL. This repository does not assume any of them.
Stage the reviewed Git tree in a new commit-named directory below that recorded
release root and verify its replacement-specific checksums before building the
hash-locked virtual environment. Do not change the `current` symlink:

```console
cd DISCOVERED_RELEASE_ROOT/releases/CANDIDATE
sha256sum --check deployment/personal-runtime/SHA256SUMS
uv venv --python /usr/bin/python3.13 .venv
uv pip install --python .venv/bin/python --require-hashes \
  -r deployment/personal-runtime/requirements.lock
uv pip install --python .venv/bin/python --no-deps --no-build-isolation .
```

Make a separate temporary runtime directory from the three examples, replace
every placeholder with reviewed non-production validation values, and run:

```console
DISCOVERED_RELEASE_ROOT/releases/CANDIDATE/.venv/bin/jarvis-personal-runtime \
  --root /path/to/inactive-validation-runtime --check
```

`--check` parses the three files and validates the complete OpenWA and private
listener configuration. It does not bind a socket, contact OpenAI or OpenWA,
write state, modify a service, or alter a network.

Do not modify the live OpenWA project during Ticket 10 validation. Do not change pairing state.
Do not change firewall rules. Do not install the unit, move the
`current` symlink, or stop the previous runtime until Ticket 12 explicitly reaches
those supervised steps.

## Installation preparation

The following commands are for the approved Ticket 12 maintenance window, not
for Ticket 10 validation. Record the exact immutable previous release, previous
service name, and previous OpenWA webhook destination before any change.

Confirm the recorded service identity and paths do not collide with the previous
runtime. Create the approved unprivileged service identity if it does not already
exist, then create the private runtime directory:

```console
sudo useradd --system --home-dir DISCOVERED_RUNTIME_ROOT \
  --shell /usr/sbin/nologin DISCOVERED_SERVICE_USER
sudo install -d -o DISCOVERED_SERVICE_USER -g DISCOVERED_SERVICE_GROUP -m 0700 \
  DISCOVERED_RUNTIME_ROOT
```

After the exact candidate is approved, atomically point `current` to its immutable
commit-named release directory:

```console
sudo ln -sfn DISCOVERED_RELEASE_ROOT/releases/CANDIDATE \
  DISCOVERED_RELEASE_ROOT/current.new
sudo mv -T DISCOVERED_RELEASE_ROOT/current.new DISCOVERED_RELEASE_ROOT/current
```

Copy `.env.example` as `.env`, `jarvis.toml.example` as `jarvis.toml`, and
`SYSTEM.md.example` as `SYSTEM.md`, then fill them through `sudoedit`. Never put
real secrets in the repository, shell history, command output, or the journal.

```console
cd DISCOVERED_RUNTIME_ROOT
sudo chown root:DISCOVERED_SERVICE_GROUP .env
sudo chmod 0440 .env
sudo chown DISCOVERED_SERVICE_USER:DISCOVERED_SERVICE_GROUP jarvis.toml SYSTEM.md
sudo chmod 0600 jarvis.toml SYSTEM.md
sudo -u DISCOVERED_SERVICE_USER \
  DISCOVERED_RELEASE_ROOT/current/.venv/bin/jarvis-personal-runtime \
  --root DISCOVERED_RUNTIME_ROOT --check
```

Render the four `@...@` placeholders in `jarvis-personal-runtime.service` with
the recorded values into an inactive temporary file. Reject values containing
whitespace, `|`, `%`, or shell metacharacters; both roots must be absolute paths
and the service identity must already exist. Review the complete rendered file,
then verify and install it:

```console
sed \
  -e 's|@SERVICE_USER@|DISCOVERED_SERVICE_USER|g' \
  -e 's|@SERVICE_GROUP@|DISCOVERED_SERVICE_GROUP|g' \
  -e 's|@RELEASE_ROOT@|DISCOVERED_RELEASE_ROOT|g' \
  -e 's|@RUNTIME_ROOT@|DISCOVERED_RUNTIME_ROOT|g' \
  DISCOVERED_RELEASE_ROOT/current/deployment/personal-runtime/jarvis-personal-runtime.service \
  > /path/to/inactive-rendered-jarvis-personal-runtime.service
systemd-analyze verify /path/to/inactive-rendered-jarvis-personal-runtime.service
sudo install -o root -g root -m 0644 \
  /path/to/inactive-rendered-jarvis-personal-runtime.service \
  /etc/systemd/system/jarvis-personal-runtime.service
sudo systemctl daemon-reload
```

Do not use `EnvironmentFile=` for `.env`: the application reads the private file
itself, and the unit's `UMask=0077` keeps newly created runtime data private.

## Private OpenWA handoff

Set `listener_host` to the exact RFC1918 IPv4 gateway address of the Docker bridge
that contains OpenWA, and set a reviewed unprivileged port. The runtime refuses
wildcard, loopback, public, hostname, and IPv6 listener values. Configure OpenWA's
single webhook destination as `http://BRIDGE_GATEWAY:PORT/webhook` only during the
supervised cutover. No public reverse proxy, host-wide bind, new Compose service,
or firewall opening is part of this package.

Before changing the handoff, verify and record OpenWA container identity, start
time, volume, networks, health, named-session `ready` state, and absence of
`LOGOUT`. Inspect the effective OpenWA `SSRF_ALLOWED_HOSTS` value as well. The
pinned gateway rejects private webhook destinations unless their literal host is
on that allowlist. Preserve the legacy rollback hostname and add only the exact
reviewed bridge gateway:

```console
SSRF_ALLOWED_HOSTS=inbound-receiver,BRIDGE_GATEWAY
```

This live OpenWA configuration change requires its own operator approval. Back up
the root-owned Compose file, edit only that value, validate with `docker compose
config --quiet`, and perform one controlled OpenWA recreation. Require the same
`openwa-data` volume, `healthy` container state, configured named session `ready`,
and absence of `LOGOUT` before changing the webhook. A fresh QR, pairing loss,
identity mismatch, or any unexpected network/exposure change stops the cutover
and restores the reviewed Compose backup. Do not scan a QR code or change Baileys
data. Do not recreate OpenWA except for this explicitly approved allowlist change.

## Start, stop, and status

Use these only after the candidate, configuration, unit, and private handoff have
been approved:

```console
sudo systemctl start jarvis-personal-runtime
sudo systemctl stop jarvis-personal-runtime
sudo systemctl restart jarvis-personal-runtime
sudo systemctl status jarvis-personal-runtime --no-pager
sudo journalctl -u jarvis-personal-runtime --since today --no-pager
```

Starting the service is not acceptance. Ticket 12 still requires phone receipt,
real command and approval checks, trace evidence, preserved OpenWA readiness and
pairing, and the operator's final go/no-go. Keep the previous runtime installed
and stopped throughout those checks.

## Rollback

If any replacement gate fails, stop the replacement first. Restore the recorded
previous OpenWA webhook destination exactly, restore the `current` symlink only
if the previous runtime used it, and start the immutable previous runtime by its
recorded service name. Do not run both assistant runtimes against one handoff.

```console
sudo systemctl stop jarvis-personal-runtime
# Human: restore the exact recorded previous webhook destination.
# Human: restore the recorded previous release pointer if applicable.
sudo systemctl start RECORDED_PREVIOUS_SERVICE
sudo systemctl status RECORDED_PREVIOUS_SERVICE --no-pager
```

Then re-verify OpenWA health plus named-session `ready`, unchanged container and
volume identity, unchanged pairing state, no `LOGOUT`, and no message replay.
Leave the failed replacement stopped and preserve its trace for diagnosis. A
rollback never removes OpenWA volumes, re-pairs the account, changes firewall
rules, deletes the candidate, or retires the previous runtime.
