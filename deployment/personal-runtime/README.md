# Native personal runtime package

This directory packages the replacement as one native Ubuntu `systemd` service.
It does not install, enable, start, stop, or replace any service by itself. Ticket
12 owns live installation and cutover with the authorized operator present.

The service reads `.env`, `jarvis.toml`, and `SYSTEM.md` directly from
`/var/lib/jarvis-personal-runtime`. The service account owns that directory. The
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

On the target Ubuntu host, stage the reviewed Git tree in a new release directory
named by its commit and verify its replacement-specific checksums before building
the hash-locked virtual environment. Do not change the `current` symlink:

```console
cd /opt/jarvis-personal-runtime/releases/CANDIDATE
sha256sum --check deployment/personal-runtime/SHA256SUMS
uv venv --python /usr/bin/python3.13 .venv
uv pip install --python .venv/bin/python --require-hashes \
  -r deployment/personal-runtime/requirements.lock
uv pip install --python .venv/bin/python --no-deps .
```

Make a separate temporary runtime directory from the three examples, replace
every placeholder with reviewed non-production validation values, and run:

```console
/opt/jarvis-personal-runtime/releases/CANDIDATE/.venv/bin/jarvis-personal-runtime \
  --root /path/to/inactive-validation-runtime --check
systemd-analyze verify \
  /opt/jarvis-personal-runtime/releases/CANDIDATE/deployment/personal-runtime/jarvis-personal-runtime.service
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

Create the unprivileged service identity and private runtime directory once:

```console
sudo useradd --system --home-dir /var/lib/jarvis-personal-runtime \
  --shell /usr/sbin/nologin jarvis-personal-runtime
sudo install -d -o jarvis-personal-runtime -g jarvis-personal-runtime -m 0700 \
  /var/lib/jarvis-personal-runtime
```

After the exact candidate is approved, atomically point `current` to its immutable
commit-named release directory:

```console
sudo ln -sfn /opt/jarvis-personal-runtime/releases/CANDIDATE \
  /opt/jarvis-personal-runtime/current.new
sudo mv -T /opt/jarvis-personal-runtime/current.new \
  /opt/jarvis-personal-runtime/current
```

Copy `.env.example` as `.env`, `jarvis.toml.example` as `jarvis.toml`, and
`SYSTEM.md.example` as `SYSTEM.md`, then fill them through `sudoedit`. Never put
real secrets in the repository, shell history, command output, or the journal.

```console
cd /var/lib/jarvis-personal-runtime
sudo chown root:jarvis-personal-runtime .env
sudo chmod 0440 .env
sudo chown jarvis-personal-runtime:jarvis-personal-runtime jarvis.toml SYSTEM.md
sudo chmod 0600 jarvis.toml SYSTEM.md
sudo -u jarvis-personal-runtime \
  /opt/jarvis-personal-runtime/current/.venv/bin/jarvis-personal-runtime \
  --root /var/lib/jarvis-personal-runtime --check
sudo install -o root -g root -m 0644 \
  /opt/jarvis-personal-runtime/current/deployment/personal-runtime/jarvis-personal-runtime.service \
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
`LOGOUT`. Never recreate OpenWA, scan a new QR code, or change its Baileys data.

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
