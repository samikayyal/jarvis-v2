# Personal runtime replacement seam inventory

## Decision

Build the replacement as a new `src/jarvis_personal_runtime/` package with its
own focused tests and, later, its own native-service assets. It may coexist in
Git with `src/jarvis_control_plane/`, but it must not import that package, open
its databases, use its protocol keys, join its Compose service graph, or reuse
its broker, persistence, connector, worker, audit, trace, archive, backup, or
recovery types.

The old runtime and all of its operational assets remain intact as rollback
material until ticket 12 passes live acceptance and the operator explicitly
authorizes ticket 13. OpenWA is not part of the replacement: its container,
configuration, databases, `openwa-data` volume, Baileys pairing, account,
session, API credentials, host exposure, and operating runbooks remain in
place.

## Path-level inventory

### Retain unchanged

| Path or external asset | Treatment | Source of truth |
| --- | --- | --- |
| `docs/openwa/` | Retain permanently. These four documents describe the independently operated gateway, its verified deployment, readiness, recovery, and acceptance evidence. | [`docs/openwa/deployment.md`](../../../docs/openwa/deployment.md), [`docs/openwa/operations.md`](../../../docs/openwa/operations.md), [`docs/openwa/verification.md`](../../../docs/openwa/verification.md) |
| `docs/adr/0001-use-baileys-as-openwa-engine.md` | Retain permanently. It fixes Baileys as the sole active engine and makes pairing state part of the complete OpenWA volume. | [`docs/adr/0001-use-baileys-as-openwa-engine.md`](../../../docs/adr/0001-use-baileys-as-openwa-engine.md) |
| `.scratch/openwa-messaging-service/` | Retain as historical gateway research and acceptance evidence; never turn it into replacement runtime code. | [gateway map](../../openwa-messaging-service/map.md) |
| `.scratch/jarvis-personal-runtime/` | Retain the execution map, issues, and research history. | [replacement map](../map.md) |
| `AGENTS.md`, `docs/agents/` | Retain repository workflow and issue/domain conventions. | [`AGENTS.md`](../../../AGENTS.md), [`docs/agents/issue-tracker.md`](../../../docs/agents/issue-tracker.md) |
| `/opt/openwa/compose.yaml`, `/opt/openwa/.env`, Docker volume `openwa-data` and everything mounted at `/app/data` | External production assets; never edit, recreate, re-pair, migrate, delete, or copy them as part of tickets 02-11. | [`docs/openwa/deployment.md`](../../../docs/openwa/deployment.md) |
| The current installed legacy release, active configuration, credentials, state and service definitions | Leave untouched until live replacement acceptance. Preserve the exact installed release separately from this mutable checkout for rollback. | [`deployment/activation-runbook.md`](../../../deployment/activation-runbook.md), ticket 12 and ticket 13 in the [replacement map](../map.md) |

### Add alongside, without compatibility imports

| New path | Coexistence rule |
| --- | --- |
| `src/jarvis_personal_runtime/` | Fresh composition root and implementation. No import from `jarvis_control_plane`, no reads of legacy state, and no compatibility facade. |
| A distinct replacement test subtree under `tests/` | Import only `jarvis_personal_runtime`. Encode the replacement contract, not the legacy ticket contracts. |
| New native-service assets introduced by ticket 10 | Use distinct filenames while the old deployment exists. Do not replace the live unit, webhook, network, or service until the ticket-12 supervised cutover. |

### Temporarily bypass; retain for rollback

| Current path | Why it is bypassed | Final treatment after accepted cutover |
| --- | --- | --- |
| `src/jarvis_control_plane/` (the complete package, including its `application/`, `domain/`, `persistence/`, `integrations/`, `workers/`, `archive/`, `diagnostics/`, and `operations/` subtrees and top-level compatibility facades) | This is the old assistant control plane. Even apparently reusable OpenWA, vault, terminal, session, and trace classes are coupled to durable ingress, the capability broker, audit, recovery, connector, or worker contracts. | Remove the complete package in ticket 13. Git history is the recovery mechanism. |
| `tests/` as it exists before replacement tests are added | The existing suite specifies `jarvis_control_plane` and the old multi-ticket architecture. A few tests do not directly import it but still validate legacy runbooks/bundles. | Remove the legacy tests and support files in ticket 13; retain only the new replacement suite. |
| `deployment/compose.yaml`, `deployment/Dockerfile`, `deployment/config.example.toml`, `deployment/requirements.lock`, `deployment/artifacts.lock.json`, `deployment/health_probe.py` | They define and pin the 13-service containerized legacy assistant. They are not a template for the one-process native runtime. | Remove after acceptance; replace active examples and guidance with the native runtime assets. |
| `deployment/activation.compose.example.yaml`, `deployment/openwa-handoff.md` | They target the old `inbound_receiver` and its container aliases/networks. They are useful cutover evidence but not directly reusable. | Supersede with the ticket-10/12 native host-bridge handoff and remove the obsolete files in ticket 13. |
| `deployment/systemd/jarvis-ubuntu-worker.service`, `deployment/windows/install-jarvis-worker.ps1` | The replacement uses local Ubuntu subprocesses and ordinary OpenSSH over Tailscale, not custom workers. | Remove after acceptance and removal of the installed legacy workers is explicitly authorized. |
| `deployment/systemd/jarvis-backup.service`, `deployment/systemd/jarvis-backup.timer`, `deployment/ticket33-*`, `deployment/*acceptance-runbook.md` | These implement legacy databases, services, recovery, and acceptance. They remain rollback/operational evidence during coexistence. | Remove or replace with replacement-specific guidance after acceptance; do not claim they protect replacement state. |
| `README.md`, `deployment/README.md`, `CONTEXT.md` | They describe the old runtime or the broader former domain. Keep them stable while the old release is a rollback candidate. | Rewrite for the accepted replacement in ticket 13. Preserve the narrow messaging vocabulary and OpenWA distinction. |
| `pyproject.toml` | It is the shared packaging seam. Add only dependencies required by the fresh package through `uv`; do not make the new package depend on legacy libraries. | Remove `openai-agents` and `pyyaml` only when no surviving replacement code needs them. Keep project/build metadata needed by the surviving package. |

The deferred-removal boundary is intentionally coarse. Selecting individual
legacy helpers for survival would create the compatibility layer that the map
forbids. The narrow OpenWA facts below may be reimplemented in small replacement
modules; the old modules themselves still belong to the removal set.

## Coexistence constraints

1. **The source artifact pin will diverge.**
   `src/jarvis_control_plane/operations/deployment_artifacts.py` hashes
   `pyproject.toml`, `README.md`, and every `src/**/*.py`. Adding
   `src/jarvis_personal_runtime/` therefore makes the mutable checkout differ
   from `deployment/artifacts.lock.json`. Do not repin that legacy lock to the
   in-progress replacement. Verify or roll back the old runtime from its exact
   immutable installed release; ticket 10 must create replacement-specific
   packaging and pins.
2. **There is one live message consumer.** The old provisioning contract allows
   exactly one active `message.received` webhook and fails on a competitor
   ([`src/jarvis_control_plane/integrations/openwa/webhook.py`](../../../src/jarvis_control_plane/integrations/openwa/webhook.py)). Tickets 03-11 use
   controlled inputs only. Ticket 12 must stop admission to the old runtime,
   switch the one webhook/handoff to the native listener under operator
   supervision, and leave the old release stopped but recoverable until final
   acceptance. Running both consumers is not a coexistence strategy.
3. **No state migration.** Do not read or transform legacy SQLite state,
   sessions, conversations, memory, audit, traces, deleted archives, dispatch
   records, permissions, backups, or recovery metadata. The replacement starts
   with in-memory sessions, its own seven-day message-ID cache, its own runtime
   trace, and only the configured saved-permission section of its own TOML.
4. **No production discovery by mutation.** Exact non-secret identities and
   network addresses are read and recorded during deployment preparation.
   Secrets remain human-supplied through the new `.env`; existing repository or
   service credential files must not be copied, printed, or rewritten.
5. **No live OpenWA changes before ticket 12.** Source work, controlled tests,
   and offline service validation do not authorize container recreation,
   webhook replacement, network attachment, firewall changes, service
   installation, or pairing.

## Narrow OpenWA facts to reimplement

These are facts, not a reusable connector interface.

### Independently verified gateway facts

| Fact to preserve | Evidence |
| --- | --- |
| OpenWA is pinned to `v0.12.1`, upstream commit `31c5499a9beea1c5b460a4854ed68587b25f53d2`, and image digest `sha256:c052dc03d3bfca490fa41f40e99aa13604239cef9c62c05f72762ef633fda85a`. | [`docs/openwa/deployment.md`](../../../docs/openwa/deployment.md) |
| The only active engine is `baileys`; its authorization is under `/app/data/baileys` inside the complete `openwa-data` volume. Preserve `ENGINE_TYPE=baileys`, `BAILEYS_AUTH_DIR=/app/data/baileys`, the separate `/app/data/sessions` rollback path, SQLite/local media, automatic session startup, `restart: unless-stopped`, and `stop_grace_period: 45s`. | [`docs/openwa/deployment.md`](../../../docs/openwa/deployment.md), [Baileys ADR](../../../docs/adr/0001-use-baileys-as-openwa-engine.md) |
| Container health at `/api/health/ready` is necessary but insufficient. The exact authenticated configured session must also have status `ready`; the internal session ID and human-readable name are not interchangeable. | [`docs/openwa/deployment.md`](../../../docs/openwa/deployment.md), [`docs/openwa/operations.md`](../../../docs/openwa/operations.md) |
| API routes that take `:sessionId` use the internal ID. The independently exercised outbound procedure used `POST /api/sessions/:sessionId/messages/send-text`; physical receipt plus database `sent` must not be described as a recorded `delivered` receipt. | [`docs/openwa/operations.md`](../../../docs/openwa/operations.md), [`docs/openwa/verification.md`](../../../docs/openwa/verification.md) |
| The gateway remains a single `openwa-api` service on port 2785, bound only to the exact private LAN address and restricted to the trusted LAN `/24`; it is not public and has one account/session. | [`docs/openwa/deployment.md`](../../../docs/openwa/deployment.md) |

### Existing assistant handoff facts that ticket 04 must revalidate

| Candidate fact | Current evidence and caution |
| --- | --- |
| Subscribe to only `message.received`, authenticate the exact raw body with the configured signing secret, and expect `X-OpenWA-Signature`. | Implemented and controlled-tested by [`src/jarvis_control_plane/integrations/openwa/webhook.py`](../../../src/jarvis_control_plane/integrations/openwa/webhook.py), [`src/jarvis_control_plane/domain/ingress_messaging.py`](../../../src/jarvis_control_plane/domain/ingress_messaging.py), and [`tests/ticket13/helpers.py`](../../../tests/ticket13/helpers.py). Recreate the minimal raw-body verification; do not import the legacy envelope, durable admission, broker, or recovery types. Confirm the actual live header/payload during ticket-12 acceptance. |
| The controlled payload contains top-level `event`, `sessionId`, `idempotencyKey`, and `deliveryId`, with `data.id`, `from`, `chatId`, `body`, `type`, `fromMe`, and `isGroup`. | [`tests/ticket13/helpers.py`](../../../tests/ticket13/helpers.py) is contract evidence, not independent gateway acceptance evidence. Ticket 04 must tolerate only the reviewed direct-text shape and reject/ignore everything else. |
| The old connector uses `X-API-Key`, 5-second HTTP timeouts, a 64 KiB response bound, a 4,096-character text envelope, and `POST /sessions/:sessionId/messages/reply` with `chatId`, `quotedMessageId`, and `text`. | [`src/jarvis_control_plane/integrations/openwa/models.py`](../../../src/jarvis_control_plane/integrations/openwa/models.py), [`src/jarvis_control_plane/integrations/openwa/connector.py`](../../../src/jarvis_control_plane/integrations/openwa/connector.py), and [`tests/ticket13/test_02_outbound.py`](../../../tests/ticket13/test_02_outbound.py). These are legacy assistant choices. The `messages/reply` route differs from the independently verified `messages/send-text` procedure, so ticket 04 must choose and live-verify the smallest supported route rather than copying the connector. |
| A send timeout or transport failure after a POST is uncertain and must not be retried automatically. | The legacy connector models this correctly in [`tests/ticket13/test_02_outbound.py`](../../../tests/ticket13/test_02_outbound.py), and the replacement map independently requires one send attempt per chunk. Reimplement the behavior without outbound recovery records. |

## Configuration preservation list

The replacement configuration must carry only the following semantic values;
field names and file layout are defined by ticket 03 and installation values
are supplied/reviewed by the operator in tickets 10 and 12.

| Preserve exactly from the reviewed live environment | Do not carry forward |
| --- | --- |
| Authorized operator WhatsApp number and OpenWA operator conversation/chat ID | Legacy `operator_id`, conversation archive IDs, broker request IDs, or state-store keys |
| OpenWA API base URL, API key, internal session ID, human-readable session name, and webhook signing secret | Protocol HMAC keys between legacy services, service identities, connector allowlists, or legacy network topology |
| The one active webhook event and its cutover destination, after ticket-04 verification | Old `inbound_receiver` role, durable admission queue, recovery claims, outbound attempt records, or replay logic |
| OpenAI API key; allowed/default model and reasoning settings agreed by the replacement map/research | Agents SDK configuration or orchestration-agent state |
| Native listener host/port reachable only through the Docker host bridge | Public assistant endpoints, the old two-container handoff aliases, or OAuth exposure |
| Vault root, Ubuntu default working directory, Windows Tailscale SSH host/user/key/working directory, timeouts, bounds, read-only prefixes, and saved host-plus-prefix permissions | Vault connector/repository abstractions, worker identities, mTLS/CA material, registration, heartbeat, IPC sockets, Job Objects, or failover |
| Session inactivity, 100,000-token limit with `o200k_base`, tool-round/request/command/output bounds, trace rotation, and seven-day dedup retention | Durable sessions, history, memory, audits, backups, operational retention, or recovery configuration |

The example values in `deployment/config.example.toml` are placeholders and
must not seed the replacement. Real secrets are never inventoried in this
document. The repository's ignored `.env` is not evidence of the future
service credential set and must not be read or reused automatically.

## Retirement gate

Ticket 13 may remove the deferred paths only after ticket 12 records all of the
following: the new native service is installed and active; exact authorized
inbound text reaches only the replacement; deterministic and model replies are
received on the phone; Ubuntu and Windows commands satisfy their approval
contract; cancellation and traces pass; OpenWA remains `healthy` plus named
session `ready`; no QR or `LOGOUT` occurred; container identity, volume,
pairing, private exposure, and message history remain unchanged; and the
operator gives the final go/no-go and separate retirement authorization.

Until then, “replacement” means a separate inactive implementation plus an
immutable rollback release—not deletion, migration, or parallel live message
processing.
