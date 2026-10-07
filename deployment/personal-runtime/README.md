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
under `/opt/jarvis-personal-runtime/releases/`. Build from the hash-locked
requirements and install the local package without resolving additional
dependencies:

```console
cd /opt/jarvis-personal-runtime/releases/COMMIT
uv venv --python /usr/bin/python3.13 .venv
uv pip install --python .venv/bin/python --require-hashes \
  -r deployment/personal-runtime/requirements.lock
uv pip install --python .venv/bin/python --no-deps --no-build-isolation .
```

Do not place credentials in the release directory. Do not change the active
`current` symlink until the candidate, configuration, and rendered unit pass
inactive validation.

## Runtime root and configuration

### Runtime skills

The installed package includes five trusted `skills/<name>/SKILL.md` resources:
`obsidian`, `google`, `terminal`, `reminders`, and optional `web`. Their short descriptions and the
`load_skill` schema are exposed initially. Loading adds that skill's instructions
and only its configured prepared tool schemas on the next model call. Instructions
live in the request's instruction field; loader results are small acknowledgements,
so repeated loads do not duplicate the skill text in conversation history.

Loaded skills persist for follow-ups in the same working session and reset with
`/new`, inactivity expiry, the context limit, or restart. Skill loading counts as
one tool round and is included in timeout and context budgets. Existing argument
validation, approvals, and continuation handling still run in the original tools.
Only the fixed packaged registry is loadable; vault notes and tool results cannot
register skills. Manifest-selected operations outside these explicit groups
keep their existing visibility.

For an existing deployment, review the shorter `SYSTEM.md.example` against its
current prompt, retaining any operator-specific instructions. Install the new
package and shortened prompt together during the normal release procedure. Merely
replacing the prompt does not enable skill loading. The catalog is generated from
configured tools and packaged frontmatter; do not duplicate it in `SYSTEM.md`.

### Optional quick web search and public-page reading

The `web` skill exposes `search_web` and `read_web_pages` at `quick` depth.
Jarvis loads it for explicit searches and freshness or verification needs, or
to read a supplied public HTML link. Answers cite source titles and URLs and
distinguish search snippets from actually read pages. Moderate research remains
reserved for a later ticket.

Web access is off when `[web_search]` is absent or `enabled = false`. The example
TOML records the shared request allowances: quick one query/two pages, moderate
three queries/five pages, five results per query, 8,000 characters per page,
32,000 evidence characters, and a 20-second browser-operation timeout. Reserved
moderate settings are validated now but do not enable that depth.
Query and page attempts (including failures) count across calls and approval continuations;
a new ordinary request resets the allowance. Runtime output, tool-round, and
overall request limits also apply. There is no separate research timer.

Google blocking or CAPTCHA stops further searches in the request. A recognized
consent screen can reject optional cookies; challenges are never solved or
retried. Empty results, changed layouts, access failures, and exhausted allowances
have distinct outcomes. Browser HTML fixtures require no live Google access;
they do not establish that Google will accept searches from the production host.

Provision compatible Chromium and ChromeDriver on the Ubuntu host before
enabling this feature. Both binaries must be installed explicitly by the
operator; the runtime never runs a package manager or Selenium Manager and never
downloads drivers. Check browser and driver versions under the service account.
Discovery accepts `/usr/bin/chromium`, `/usr/bin/chromium-browser`,
`/usr/bin/google-chrome-stable`, or `/usr/bin/google-chrome`, with
`/usr/bin/chromedriver` or `/usr/local/bin/chromedriver`. Pin a mutually compatible
browser/driver pair through the host's provisioning process and verify both with
`--version`; an arbitrary driver on the inherited shell `PATH` is not used.
The Chromium sandbox must work for that account; do not use `--no-sandbox`.
Missing or incompatible binaries produce an unavailable-web outcome. No browser
provisioning or production activation is implied by installing this package.

The request owns a separate Python worker and one anonymous browser profile in
the private runtime directory. Browser work does not use terminal permissions,
Google Workspace tokens, personal browser profiles, or `openwa-data`. It returns
bounded visible HTML text, titles, and URLs; PDFs, downloads, authenticated pages,
and general browser actions are unsupported. Failed reads are not retried.
Page JavaScript is disabled in this quick reader; content that requires page
scripts may be incomplete or unavailable. Selenium's own bounded extraction
still reads visible rendered text. Every HTTP(S) connection, including redirects
and subresources, passes through a local proxy that rejects non-public DNS
answers and connects to the validated address. Non-HTTP schemes and downloads
are blocked by Chromium controls. No private-destination bypass is configurable.
Completion, approval suspension, cancellation, and shutdown close the worker's
process group and remove its profile. A failed browser stays unavailable for
the rest of that request. Partial evidence remains usable with a coverage notice.

Use the existing config editor/restart procedure to set `enabled = true` only
after the host prerequisites are checked. Validate with the packaged CLI's
`--check`, restart normally, and exercise a direct public link. Disable by setting
`enabled = false` and restarting; roll back the release through the existing
stopped-service rollback procedure. Runtime checks do not prove live web egress
or end-to-end WhatsApp readiness.

### Configuration files

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
`[saved_permissions]` section of `/etc/jarvis/jarvis.toml`. Runtime-owned cache,
Reminder database, and trace paths must stay below the runtime root. The
required `operator_timezone` is an IANA name and is initially `Asia/Amman`; it
does not inherit the host timezone. `vault_path` may be an absolute directory
outside the runtime root. Without `[vault_git]`, it remains read-only. With
`[vault_git]`, it must be the service-owned dedicated clone used for the
bounded Markdown-only `edit_vault` workflow.

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

Install the transactional editor once:

```console
sudo install -o root -g root -m 0755 \
  deployment/personal-runtime/modify-jarvis-config \
  /usr/local/bin/modify-jarvis-config
```

Thereafter run `modify-jarvis-config`. It stops Jarvis to prevent concurrent
saved-permission writes, edits a temporary copy, validates it, atomically
promotes it, and restarts the service. Invalid edits are retained only in the
root-only backup directory and the prior valid configuration is restarted.

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
authorized operator number and chat ID, `operator_timezone`,
`reminder_database_path`, Ubuntu working directory, and read-only prefixes.
Configure all four
Windows SSH fields together when Windows execution is enabled. The identity file
must be private to the service account and the SSH host key must already be
pinned in that account's known-hosts file.

### Optional Git-backed Obsidian vault

Add this section only when Jarvis is authorized to edit and synchronize the
dedicated vault clone. The section requires `remote`, `branch`, and
`note_directories`, plus `vault_path` as the clone root. The author identity has
defaults; SSH identity and known-hosts paths must be configured together:

```toml
# Add this to the [runtime] section.
vault_path = "/srv/knowledge-vault"

[vault_git]
remote = "git@github.com:ACCOUNT/OBSIDIAN-VAULT.git"
branch = "main"
note_directories = ["Projects", "Ideas"]
author_name = "Jarvis"
author_email = "jarvis@samikayyal.com"
ssh_identity_file = "/var/lib/jarvis-personal-runtime/credentials/obsidian-vault"
ssh_known_hosts_file = "/var/lib/jarvis-personal-runtime/credentials/known_hosts"
```

The clone at `vault_path` must already exist, be owned by the service account,
and have the configured remote and branch. The SSH private key and known-hosts
file are provisioned separately with service-account-only permissions; do not
put their contents in TOML, `.env`, `SYSTEM.md`, model context, or traces. The
remote host identity must be pinned before activation. Jarvis invokes a bounded
Git argument list with this identity and known-hosts file; it does not run Git
through `run_terminal` or a shell.

`list_vault_tree` returns the recursive folder and file paths (including
attachments and empty folders) without reading file contents. It is available
with or without Git configuration. Git-backed listings synchronize first and
respect `note_directories`; standalone listings cover the configured vault root.
Hidden entries, symlinks/junctions, and nested repositories are excluded.
Start with `offset: 0`, then follow `next_offset` until null. Each page has at
most 200 entries and fits the configured tool-result limit. The scan rejects
more than 20,000 filesystem entries or unreadable directories explicitly; it
never labels a partial scan complete. Git-backed pages report freshness and
base revision; restart pagination if that revision changes between pages.

`read_vault` synchronizes a clean clone before fresh reads and includes the
base revision. A temporarily unavailable remote may produce a clearly marked
stale read, but cannot prepare a write. `edit_vault` accepts only exact
Markdown create/update changes below `note_directories`, previews additions,
removals, replacements, or new content in plain language, and freezes the proposal.
Reply `1` to approve once, `2` to always approve future edits to that exact file,
or `9` to reject; `/cancel` also discards the proposal. Choice `2` is offered
only for single-file proposals. Grants survive restart, are scoped to the clone,
remote, branch, and exact note path, and never grant terminal permission.
Use `/permissions` and `/forget-permission ID` to inspect or revoke a grant.
Future requests skip the prompt only when every changed file has a saved grant.
`delete_vault` accepts `base_revision`, `commit_message`, and one exact `path`.
It previews the entire Markdown note and always requires `1` to approve once;
`9` or `/cancel` rejects it. Saved edit grants do not authorize deletion, and
choice `2` is unavailable. Approval removes the file, commits, and syncs through
the same validated Git workflow. Deleted content remains in Git history.
Git hashes and commit messages are retained in diagnostic traces; replies simply
describe what changed and whether it synced.
After approval Jarvis revalidates the base, writes the notes, stages only the
approved paths, creates one normal commit, and pushes it. It never merges,
rebases, force-pushes, resolves conflicts, or blindly retries an uncertain
push. A local commit that the remote did not accept is preserved and reported
as `committed_not_synced`; an unverifiable push is reported as `sync_unknown`.

Keep the vault clone and credential files outside the application release
directory. Before live activation, validate the section with `--check`, prove
the temporary-clone contract, make one small supervised test edit, verify the
commit on the configured remote, and confirm the Obsidian client can pull it.
Do not treat this documentation or a successful `--check` as live acceptance.

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
When `[vault_git]` is present, `--check` also validates its required fields and
paths without contacting the Git remote or modifying the clone.

The Reminder capability is the one proactive exception to the otherwise
inbound-driven runtime. Jarvis stores operator-requested one-time Reminders in the
configured SQLite database and owns their due-time decisions. One scheduler in
the existing service process submits the exact stored body once to the
configured operator chat ID. OpenWA remains the immediate transport and owns
pairing, readiness, and message persistence; it does not own Reminder state or
timing. See the [Reminder lifecycle and supervised acceptance procedure](../../docs/reminders.md)
before testing this capability with the authorized account.

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
operator makes the final go/no-go decision. Vault activation has the additional
gates in [the vault editing spec](../../.scratch/jarvis-vault-editing/spec.md):
temporary-clone tests, a clean candidate release, one supervised note edit,
remote verification, and an explicit recovery procedure for a local commit or
an uncertain push.

## Install or update the service

To allow Jarvis to run approved Ubuntu terminal commands with unrestricted
passwordless sudo, install the dedicated sudoers rule. This gives the service
account full root access when it invokes `sudo`; Jarvis's WhatsApp command
approval still applies before execution.

```console
sudo visudo -cf deployment/personal-runtime/jarvis-personal-runtime.sudoers
sudo install -o root -g root -m 0440 \
  deployment/personal-runtime/jarvis-personal-runtime.sudoers \
  /etc/sudoers.d/jarvis-personal-runtime
sudo visudo -c
```

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
