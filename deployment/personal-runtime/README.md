# Native personal runtime deployment

The supported deployment is one native Ubuntu `systemd` service. OpenWA remains
a separate Docker Compose project and reaches the runtime through one private
Docker host-bridge listener. Do not add the assistant to OpenWA's Compose graph.

## Package verification

From a clean checkout, verify the surviving runtime before packaging:

The tracked `uv.lock` fixes the development/test environment. Runtime and build
pins in `pyproject.toml` agree with `requirements.in`; deployment installs use
the separate hash-locked `requirements.lock`. Update and compare these together
when intentionally changing dependency versions.

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

### Optional web research and public-page reading

The `web` skill exposes `search_web` and `read_web_pages` at `quick` and `moderate` depth.
Jarvis loads it for explicit searches and freshness or verification needs, or
to read a supplied public HTML link. Answers cite source titles and URLs and
distinguish search snippets from actually read pages. Moderate research compares
relevant sources and identifies material disagreements and coverage gaps.

Web access is off when `[web_search]` is absent or `enabled = false`. The example
TOML records the shared request allowances: quick one query/two pages, moderate
three queries/five pages, five results per query, 8,000 characters per page,
48,000 evidence characters, and a 20-second browser-operation timeout. Existing
deployments with an explicit 32,000 setting retain it until their TOML is updated.
The first
web call selects depth; a later moderate call upgrades quick without resetting
usage, and later quick calls cannot downgrade it. Valid configuration changes
take effect on the normal service restart.

`browser_mode = "headless"` is the default. The opt-in `"headed"` mode requires
Ubuntu packages `xvfb` and `xauth` (provision with `sudo apt-get install xvfb xauth`).
It starts a private 1280x900 Xvfb display in each worker's supervised process
group, uses headed Chrome, and disables the Blink `AutomationControlled` feature.
The sandbox, fresh HOME/profile, public-network proxy, script restrictions and
challenge-stop behavior remain enabled. No personal desktop/session is reused.
Missing Xvfb fails the request as unavailable; it does not silently retry in
another browser mode. Neither mode guarantees Google acceptance. Roll back by
restoring `browser_mode = "headless"` and restarting; before rolling back to a
release that predates this setting, remove the setting from TOML as well.

Some Google results use opaque `/goto?url=...` redirects. Search evidence preserves
the actual returned link; it does not invent a destination from a shortened
display URL. Reading that link consumes the normal page allowance and validates
its redirects through the public-network proxy. A successful read supplies the
final source URL for citations. An optional filter/feedback dialog alone is not
classified as a CAPTCHA.

Query and page attempts (including failures) count across calls and approval continuations;
a new ordinary request resets the allowance. Runtime output, tool-round, and
overall request limits also apply. There is no separate research timer.
Follow-ups can reuse evidence from the working-session transcript without network
work. Explicit rechecks and time-sensitive claims require fresh tools. Every
ordinary request owns and closes its browser; no browser or separate evidence
cache survives across requests. Session expiry, `/new`, context reset, and restart
discard the old working-session evidence and loaded skills.

Page reads default to static content. `read_web_pages` also accepts explicit
`render_mode="rendered"` with `wait_for_text` (1–200 characters) after a static
read of the same URL. Rendering consumes another page attempt and shares all
request limits. A separate anonymous worker permits scripts while retaining
the public proxy, sandbox, scheme/download blocks and supervised cleanup. Its
bounded wait observes visible text, never clicks controls or solves challenges.
Blocked/unsupported URLs cannot be render-retried in the same request. Both
workers close on suspension/cancellation/completion; counters survive suspension.

Results distinguish access from extraction: `truncated`, `source_chars`,
`extraction_method`, `section_found`, `render_mode`, `render_wait`, and
`stop_reason` describe evidence limits and readiness. Anchor presence does not
mean that section was included: extraction remains a body prefix. Start with
one or two sources and adapt; retain larger batches for known independent sources.
Final notices distinguish search blocks, page failures and allowance limits.

Google blocking or CAPTCHA stops further searches in the request. A recognized
consent screen can reject optional cookies; challenges are never solved or
retried. Empty results, changed layouts, access failures, and exhausted allowances
have distinct outcomes. Browser HTML fixtures require no live Google access;
they do not establish that Google will accept searches from the production host.

Provision compatible Chromium and ChromeDriver on the Ubuntu host before
enabling this feature. Both binaries must be installed explicitly by the
operator; the runtime never runs a package manager or Selenium Manager and never
downloads drivers. Check browser and driver versions under the service account.
Discovery prefers `/usr/bin/google-chrome-stable`, then `/usr/bin/google-chrome`,
`/usr/bin/chromium`, and `/usr/bin/chromium-browser`, with
`/usr/local/bin/chromedriver` preferred over `/usr/bin/chromedriver`. This lets an
explicit native pair take precedence over Ubuntu's Snap compatibility launchers.
Pin a mutually compatible
browser/driver pair through the host's provisioning process and verify both with
`--version`; an arbitrary driver on the inherited shell `PATH` is not used.
The Chromium sandbox must work for that account; do not use `--no-sandbox`.
Missing or incompatible binaries produce an unavailable-web outcome. No browser
provisioning or production activation is implied by installing this package.

The request owns a separate Python worker and one anonymous browser profile in
the private runtime directory. Its fresh `HOME` is inside that profile so native
Chrome can create auxiliary state without inheriting the service account's home;
both are removed together. Browser work does not use terminal permissions,
Google Workspace tokens, personal browser profiles, or `openwa-data`. It returns
bounded visible HTML text, titles, and URLs; PDFs, downloads, authenticated pages,
and general browser actions are unsupported. Failed reads are not retried.
Page JavaScript is disabled for ordinary reads; content that requires page
scripts may be incomplete or unavailable. Search permits scripts only on the
HTTPS `www.google.com` and `consent.google.com` origins, then disables execution
before ordinary page reads. Search stops at Google's anti-bot challenges and
reports the specific navigation failure when available. Enabling JavaScript
does not guarantee that Google will accept automated traffic from the host.
Selenium's own bounded extraction
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

#### Provision and verify a browser candidate

Provision the browser independently of runtime requests. On native Ubuntu, use
a reviewed Google Chrome `.deb` plus its compatible ChromeDriver, or a matching
Chrome for Testing browser/driver release. For Chrome 115 and later, use Google's
[version selection guide](https://developer.chrome.com/docs/chromedriver/downloads/version-selection)
and the [Chrome for Testing availability dashboard](https://googlechromelabs.github.io/chrome-for-testing/).
Select the driver for the installed browser's `MAJOR.MINOR.BUILD`, recording the
full versions and downloaded artifact checksums. Matching only the major version
does not establish compatibility. Keep the matching pair for rollback; browser
auto-updates require a new compatibility check.

For an explicitly selected Chrome for Testing pair, unpack each archive under a
versioned `/opt/jarvis-browser/VERSION/` directory and install its required native
libraries through the Ubuntu package manager. Point `/usr/bin/google-chrome` at
that release's `chrome-linux64/chrome` and `/usr/local/bin/chromedriver` at
`chromedriver-linux64/chromedriver`. The earlier browser candidate
`/usr/bin/google-chrome-stable` takes precedence if present; verify the probe's
resolved browser and driver paths select the intended pair. Both must be
executable by `jarvis-personal-runtime`; the versioned directory must be readable
and traversable. Do not point at a personal Chrome profile.

Ubuntu's `chromium-browser` package can be a Snap launcher. Verify that the
actual browser, its driver, confinement, and private profile path work together
under the service identity before choosing this route. An unpacked browser on
Ubuntu 24.04 can also require an administrator-reviewed AppArmor policy to allow
its sandbox/user namespaces. Do not disable the Chromium sandbox or globally
weaken the host to make a smoke check pass. A `--version` result alone does not
test sandbox startup.

The supplied unit uses the service UID/GID, `UMask=0077`, the private runtime
working directory, and no interactive desktop. Test under these same constraints.
The worker deliberately drops inherited credentials, `HOME`, proxy settings, and
user-selected `PATH` entries; only explicit discovery paths above are used. It
needs permission to create a private profile, loopback sockets for ChromeDriver
and the restrictive proxy, and public DNS/HTTP(S) egress. Extra service hardening
must allow Chromium's sandbox and worker process groups. Containers have separate
seccomp/user-namespace constraints and do not establish native systemd acceptance.

From the candidate release, run the optional installed-package probe as the
service account, outside the source checkout:

```console
sudo -u jarvis-personal-runtime sh -c 'umask 077; cd /var/lib/jarvis-personal-runtime; \
  uv run --no-project --python /opt/jarvis-personal-runtime/releases/COMMIT/.venv/bin/python \
  python /opt/jarvis-personal-runtime/releases/COMMIT/deployment/personal-runtime/verify-web.py \
  --live --output web-verification.json'
```

The probe records installed package paths, pins, executable versions, a real
Google query, a selected IANA page read, a session follow-up, a fresh recheck, and
profile removal. Its Responses provider is scripted: it spends nothing on model
or search services and demonstrates tool/evidence flow, not live-model judgment.
Inspect the JSON outcomes and supporting excerpts. Exit zero means the probe
finished; it does **not** mean Google succeeded. `blocked`, CAPTCHA,
`extraction_failed`, `timeout`, or `unavailable` remain failed lookup evidence.
The normal runtime fallback notice must accompany a reply with no usable web
evidence; a genuinely empty search has a different notice. Never fabricate a
successful lookup or solve/retry a CAPTCHA to pass acceptance.

The opt-in browser tests are independent of Google:

```console
JARVIS_TEST_CHROMIUM_BINARY=/usr/bin/google-chrome \
JARVIS_TEST_CHROMEDRIVER_BINARY=/usr/local/bin/chromedriver \
uv run pytest tests/personal_runtime/test_web_browser.py \
  tests/personal_runtime/test_web_installed_browser.py -q
```

Their fixture routing is confined to test code and temporary child entrypoints;
there is no production setting permitting private destinations. Run the copied
tests from a directory outside the checkout with a wheel-only environment to
verify the installed artifact. Test tools such as pytest belong in the isolated
verification environment, not the service's runtime dependencies.

#### Enable, disable, and roll back web access

1. Stage the package and compatible browser pair. Validate the candidate TOML
   with the packaged CLI `--check` as described below. It validates configuration;
   it does not launch Chromium or query Google. Positive finite limits and timeout
   values are required, quick counts cannot exceed moderate counts, and total
   evidence capacity cannot be smaller than the per-page bound.
2. Complete the browser probe and controlled fixture checks in the test
   environment. Record any missing live success evidence. The ordinary default
   request timeout is 600 seconds and the tool-round ceiling is eight, including
   skill loading; these remain separate from the 20-second browser operation
   bound. There are no quick/moderate timers or promised answer times.
3. For a separately authorized production activation, use `modify-jarvis-config`
   to set `[web_search].enabled = true`. Its stop/edit/validate/promote/start
   sequence makes the immutable configuration snapshot effective. Check service
   status and then obtain one real authorized WhatsApp search with a supported
   citation in the phone reply. Configuration and binaries alone are insufficient.
4. To disable, set `enabled = false` through the same validated restart flow.
   The web skill/tools disappear; the existing assistant tools and messaging
   gateway retain their configuration. Restart discards the working session and
   any active or pending request; it does not replay them.
5. For rollback, stop the native service, restore the prior reviewed release
   symlink and matching configuration/prompt/browser pair if those changed,
   validate with that release's CLI, then start and verify it. Preserve existing
   saved permissions and runtime data. Do not change OpenWA's image, pairing,
   volume, credentials, or deployment. Use the general rollback procedure below.

All search/page traffic is anonymous public browsing. There are no paid search
APIs, paid proxies, extra model services, Workspace credentials, or OpenWA browser
state in this path. Existing assistant model usage is a separate expense.

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
