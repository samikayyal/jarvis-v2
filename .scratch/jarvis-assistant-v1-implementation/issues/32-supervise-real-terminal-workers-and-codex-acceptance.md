# 32 — Supervise real Ubuntu, Windows, terminal, and Codex acceptance

**What to build:** Under direct human supervision, demonstrate both authenticated workers, deterministic terminal authority, bounded execution, and Codex specialization without allowing Jarvis or Codex to activate trust-critical components.

**Blocked by:** 30 — Supervise initial activation and OpenWA handoff acceptance.

**Status:** ready-for-human

- [ ] Natural-language host selection, both authenticated workers, offline behavior, identity mismatch, reconnection, and no-failover behavior pass against the real hosts.
- [ ] Safe reads, ordinary approval, mandatory-fresh approval, exact session/persistent permission, revocation, timeout, truncation, partial outcome, and process-tree cancellation are demonstrated.
- [ ] Prompt-injection containment and independently verified Codex preparation/testing pass while push, approval bypass, broad sandboxing, and trust-critical activation remain impossible.

## Comments

### Sanitized supervised preflight — 2026-08-24

- Added the post-activation `deployment/terminal-codex-acceptance-runbook.md`
  worksheet with explicit real-host, terminal-authority, bounded-execution,
  prompt-injection, Codex, stop, reconciliation, and human-ownership gates.
- Fresh `/status` and `/permissions` controls passed in session `S-072`: the
  request and pending-action slots were idle, command permissions were zero,
  Ubuntu and Windows were ready, OpenWA was ready, and the connected-service
  readiness projection was healthy.
- The first host-neutral safe-read request failed at the orchestration service
  before proposal or action. Jarvis reported that no action was taken under
  `orchestration-failure-2f18625d924b4f60a3d21310e823678a`, then reconciled to
  idle with both workers still ready and zero permissions.
- At the operator's direction, the session model was changed from Terra/medium
  to Luna/medium without changing the persistent default. One exact retry failed
  at the same boundary under
  `orchestration-failure-f2af961e73a64c3eaa24810726897b5e`; Jarvis again
  reported that no action was taken.
- The Windows-dependent request and every mutating, approval, permission,
  lifecycle, failure-injection, bounded-output, cancellation, and Codex row were
  not attempted after the safe-read gate failed. No service, credential,
  certificate, network, active configuration, terminal target, or Codex
  workspace was changed.
- Protected Ubuntu orchestration evidence is required to diagnose the failure.
  The Tailscale route is healthy, but the current local SSH key was not accepted;
  no password, credential value, or protected trace payload was requested or
  exposed. Ticket 32 remains `ready-for-human`.

### Sanitized diagnosis and candidate repair — 2026-08-24

- Administrative access was restored by using the correct `samik` Ubuntu
  account with the existing key. The active immutable release is `f23b664`; all
  13 Jarvis services are healthy, administrative status reports application
  revision `df557b3`, Ubuntu and Windows ready, messaging ready, audit writable,
  and resource pressure `ok`.
- Protected trace metadata shows both acceptance requests ended as failed model
  turns before any worker operation. The retained exception classification is
  `model proposed fields outside terminal authority`, with
  `operation_started=false` and no possible dispatch or send ambiguity.
- The deterministic rejection is correct. The orchestration instructions state
  exact payload shapes for Gmail and vault proposals but did not state the
  existing terminal proposal shape, allowing both Terra and Luna to emit extra
  metadata that the authority boundary rejects.
- The candidate repair changes no accepted field or authorization rule. It tells
  the model that terminal payloads require only `host`, `executable`,
  `arguments`, and `cwd`, may optionally contain `components`, and must not add
  command, shell, stdin, timeout, environment, approval, permission, sandbox, or
  explanatory metadata. The existing fail-closed unknown-field check remains
  unchanged.
- The focused orchestration and Ticket 32 contract tests pass (`42 passed`). The
  broader terminal, permission, worker, Codex, protocol, deployment, and Ticket
  32 matrix produced `287 passed, 1 skipped`; its four expected deployment-lock
  failures report only that the changed application source has not yet been
  repinned. No active release or production configuration has been changed.

### Candidate pin and final verification — 2026-08-24

- The narrow repair is committed as `3639024`; the application artifact lock is
  repinned by `ee31e10` to that exact revision and source hash. The branch is
  pushed as `origin/codex/ticket32-terminal-codex-acceptance`.
- After repinning, the complete deployment-bundle file passed (`60 passed`).
  Ruff, format checking, Python compilation, and diff checking pass.
- The single final full-suite run passed with `802 passed, 2 skipped` in
  603.45 seconds. No additional full-suite run was started.
- This is candidate evidence only. No image was built on Ubuntu, no release was
  installed or activated, no active configuration or image map changed, and no
  service was recreated. Live Ticket 32 acceptance remains blocked until a
  separately authorized supervised replacement cutover and fresh safe-read gate.

### Supervised replacement attempt and rollback — 2026-08-24

- The authorized candidate archive matched its local and Ubuntu SHA-256, was
  extracted as the root-owned release `3565391`, and passed the artifact,
  active-configuration, and base Compose validators. Its locked Python
  environment installed successfully with `uv` and all 13 candidate-specific
  images built successfully.
- The rendered candidate model differed from the active model in exactly the 13
  image references and in no other field. A separate pre-existing pending
  override was preserved untouched. The pre-change backup
  `20260824T013225.917811Z-pre-change` remained available before activation.
- The 13 candidate services were activated with `--no-build --pull never` and
  initially reached healthy state. Administrative status reported revision
  `3639024`, both workers ready, messaging ready, audit writable, and resource
  pressure `ok`; the running image IDs were recorded and a verified baseline
  backup was created as `20260824T014954.627073Z-nightly`.
- The subsequent host-side gate caught one restart of `google_egress_proxy` and
  reported that component unavailable. The runbook treats any post-start
  restart as a hard stop, so no WhatsApp retry or Ticket 32 acceptance action was
  attempted.
- Read-only kernel evidence confirms the restart was a memory-cgroup OOM at the
  candidate proxy's 32 MiB service limit; the kernel killed its UID-10012 Python
  process. Re-running the same candidate is therefore unsafe. A new attempt
  requires a separately reviewed resource-envelope repair, artifact repin, and
  replacement authorization rather than an acceptance retry.
- Rollback restored release `f23b664`, the prior active override, image map,
  `current` and `previous` pointers. Final verification reports revision
  `df557b3`, all 13 components ready and healthy with zero restarts, both workers
  ready, messaging ready, audit writable, backup freshness current, resource
  pressure `ok`, and exact running-image/map equality.
- OpenWA retained the same container identity and start time throughout, stayed
  healthy with zero restarts, and the callback Funnel route remained unchanged.
  The exact local and Ubuntu transfer archives were removed; the candidate
  release, candidate images, protected backups, and rollback evidence remain
  available for diagnosis. Ticket 32 remains `ready-for-human`.

### Bounded egress-proxy resource repair — 2026-08-24

- The reviewed repair raises all three structurally identical egress-proxy
  memory limits from 32 MiB to 48 MiB. The aggregate Jarvis envelope is now
  1056 MiB; CPU and PID budgets are unchanged. Direct regression coverage
  asserts the 48 MiB limit for each proxy.
- The source repair is committed as `bf91bb9`; artifact-pin commit `46d2edd`
  locks that exact application revision and source hash. The focused deployment,
  egress, and Ticket 32 matrix passed (`71 passed`), and Ruff, format, Python
  compilation, and diff checks pass.
- A disposable Ubuntu probe used the existing candidate Google proxy image with
  the reviewed non-root identity, read-only filesystem, tmpfs cache, 0.03 CPU,
  16 PIDs, a 48 MiB memory-and-swap cap, the active configuration read-only, and
  `network none`. The active lightweight TCP readiness probe passed on attempt
  15; the service remained running at 39.92 MiB of 48 MiB with zero restarts and
  `oom_killed=false`. The explicitly named probe container was removed.
- Two preliminary disposable probe invocations were inconclusive: one omitted
  the active `UV_CACHE_DIR` and exited before application startup, and one used
  the bundle's generic Python health command instead of the active lightweight
  TCP override. Neither was OOM-killed, and both explicitly named containers
  were removed before the successful probe.
- The single final full-suite run after the repair passed with `802 passed, 2
  skipped` in 605.21 seconds. No second full-suite run was started.
- Post-probe verification found all 13 production containers still running; the
  production Google proxy remained healthy at its rolled-back 32 MiB limit with
  zero restarts and no OOM. OpenWA retained container ID `24fb501ab8eb`, its
  original start time, healthy state, and zero restarts. No production release,
  image map, network, credential, active configuration, service, or WhatsApp
  side effect was changed. Ticket 32 remains `ready-for-human` pending a fresh
  replacement-cutover authorization.

### Authorized replacement cutover — 2026-08-24

- The synchronized `aa9104e` archive matched SHA-256 across Windows and Ubuntu
  and was installed as the immutable root-owned release
  `/opt/jarvis/releases/aa9104e42f6edacc175899b4691f61b9876a0c23`.
  Artifact, active-configuration, base Compose, locked-venv, and image-internal
  validation all passed with 13 services and the 1056 MiB reviewed envelope.
- All 13 candidate-specific images built from the pinned base images. After
  normalizing release build paths, the rendered candidate differed from the
  active model in exactly 13 image references and the three reviewed proxy
  memory limits, with no other change. The pre-existing pending override was
  preserved at SHA-256 `5f3079be2bbc`.
- The final rollback baseline had all 13 services healthy with zero restarts or
  OOMs, both workers ready, messaging ready, audit writable, backup current, and
  resource pressure `ok`. A verified pre-change backup was created at
  `/var/backups/jarvis/20260824T025015.891704Z-pre-change` immediately before
  activation.
- The active pointer now targets release `aa9104e`; `previous` targets rollback
  release `f23b664`. All 13 candidate services became healthy with zero restart
  or OOM hard stops. Administrative status reports application revision
  `bf91bb9`, both workers ready, messaging ready, audit writable, current backup
  freshness, and resource pressure `ok`.
- The exact 13-entry running image map was installed and verified at SHA-256
  `a5dcbc03c414`. A verified candidate baseline backup was created at
  `/var/backups/jarvis/20260824T025808.113584Z-nightly`. The backup timer remains
  enabled and active.
- Three fixed-interval settled-resource samples passed. Each proxy remained
  stable near 34.8 MiB under its 48 MiB cap; every service remained healthy with
  zero restarts and `oom_killed=false`. Host memory, swap, and disk headroom
  remained acceptable, and the post-cutover kernel OOM gate passed.
- Running hardening, port, and network checks passed. Jarvis publishes only the
  reviewed loopback callback and private Tailscale worker-gateway binds; OpenWA
  retains its private LAN bind. The handoff and API networks each retain exactly
  two members, and the worker overlay retains one member.
- OpenWA retained container ID `24fb501ab8eb`, its original start time, healthy
  state, and zero restarts. The Funnel remains the exact `/callback` route. The
  local and Ubuntu transfer archives were removed. No WhatsApp acceptance
  message or terminal action was sent during cutover. Ticket 32 remains
  `ready-for-human` for the supervised behavioral worksheet.

### Post-cutover supervised safe-read stop — 2026-08-24

- Fresh WhatsApp controls opened session `S-073` idle with no pending action,
  zero command permissions, both workers ready, OpenWA ready, and Google ready.
  `/revoke session` found no matching active permission. The operator-selected
  session model was restored to Luna with medium reasoning; the persistent
  default was not changed.
- The first authorized post-cutover request asked for a host-neutral,
  non-mutating operating-system safe read. Jarvis returned an orchestration
  failure under `orchestration-failure-bdf4b90098334694ace515f520da196d` and
  explicitly reported that no action was taken. The Windows request and every
  later worksheet row were not sent after this gate failed.
- Protected trace metadata classifies the underlying model turn as `model
  returned a malformed action proposal`, rather than the earlier unknown-field
  rejection. The retained exception metadata records `operation_started=false`,
  `may_have_dispatched=false`, and `may_have_sent=false`. The field-level model
  validation cause and raw malformed proposal were not retained, so an exact
  repair cannot be derived safely from this trace and no retry was attempted.
- Final protected reconciliation reports session `S-073` idle with no pending
  action and zero permissions; Ubuntu, Windows, OpenWA, and Google remain ready.
  Administrative status remains fully ready with audit writable, backup current,
  and resource pressure `ok`; all 13 services remain healthy with zero restarts
  or OOMs. Ticket 32 remains `ready-for-human` at the safe-read gate.

### Provider terminal-schema repair — 2026-08-24

- The provider-facing structured output now uses a discriminated terminal
  proposal with a closed, typed payload. It requires `host`, `executable`,
  `arguments`, and `cwd`; permits only typed `components`; and forbids extra
  terminal and component fields before deterministic proposal freezing. Gmail,
  vault, and the public adapter proposal contract remain unchanged.
- The source repair is committed as `0b203aa`; artifact-pin commit `db88088`
  locks that exact application revision and source hash. Focused orchestration
  coverage passed (`41 passed`), unchanged Gmail/vault/exclusion coverage passed
  (`61 passed`), and the deployment, egress, and Ticket 32 matrix passed (`71
  passed`). Repository-wide Ruff, format, Python compilation, and diff checks
  pass.
- The single final full-suite run completed with `806 passed, 2 skipped, 1
  failed` in 906.86 seconds. The sole failure was the unrelated Windows manual
  trace-boundary worker test; its one isolated rerun passed in 4.36 seconds.
  The full suite was not repeated.
- No production release, service, configuration, network, credential, OpenWA,
  or WhatsApp state was changed, and the failed acceptance request was not
  retried. Ticket 32 remains `ready-for-human` pending a fresh replacement
  cutover authorization and a new action-time confirmation for each WhatsApp
  acceptance message.

### Authorized terminal-schema replacement cutover — 2026-08-24

- The committed `3d07bab` archive matched SHA-256 `d8248ba46de0` across Windows
  and Ubuntu and was installed as the root-owned release
  `/opt/jarvis/releases/3d07babb34567395bb08cfacdcba7664fd20f96c`.
  Artifact, active-configuration, base Compose, locked-venv, and image-internal
  validation passed at 13 services, 1056 MiB, 1.80 CPU, and 512 PIDs.
- The pre-cutover baseline had all 13 services healthy with zero restarts/OOMs,
  both workers ready, messaging ready, audit writable, backup current, and
  pressure `ok`. The verified pre-change backup is
  `/var/backups/jarvis/20260824T043034.662946Z-pre-change`.
- The active pointer now targets release `3d07bab`; `previous` targets rollback
  release `aa9104e`. The active override SHA-256 is `187ca12e5a20`, the preserved
  pending override remains `5f3079be2bbc`, and the exact running 13-image map is
  installed at SHA-256 `1d4ca428fc16`. The verified candidate baseline backup is
  `/var/backups/jarvis/20260824T095402.821387Z-nightly`.
- All 13 candidate services are healthy with zero restarts and no OOM flags.
  Administrative status reports application revision `0b203aa`, both workers
  ready, messaging ready, audit writable, backup current, and pressure `ok`.
  Three fixed-interval resource samples passed; the proxies remained stable near
  34.9–35.3 MiB under 48 MiB, and host memory, swap, and disk headroom remained
  acceptable. The post-cutover kernel OOM gate passed.
- Running read-only filesystems, dropped capabilities, no-new-privileges,
  non-root identities, restart policy, exact published ports, and reviewed
  network member counts passed. OpenWA retained container ID `24fb501ab8eb`, its
  original start time, healthy state, zero restarts, and one named session in
  `ready`. The Funnel remains the exact `/callback` route.
- The local and Ubuntu transfer archives were removed. No WhatsApp acceptance
  message or terminal action was sent during cutover. Ticket 32 remains
  `ready-for-human` for a new supervised behavioral worksheet; each message
  requires fresh action-time confirmation.

### Post-schema-cutover safe-read policy stop — 2026-08-24

- Fresh supervised controls opened session `S-074` idle on Luna with medium
  reasoning, no pending action, zero command permissions, and Ubuntu, Windows,
  OpenWA, and Google ready. The persistent model default was not changed.
- The exact host-neutral operating-system read selected Ubuntu with the displayed
  default-host reason, but deterministic policy incorrectly presented proposal
  `request-c47604e8168841f1a20d649b87579594` instead of auto-authorizing the
  provably safe read. The proposal was rejected once with choice `4`; it was not
  approved or retried.
- A fresh `/status` containment check reported no active request, no pending
  action, and zero command permissions. Both workers, OpenWA, and Google remained
  ready. Gate 02's Ubuntu selection evidence passed, but Gate 05 failed because a
  safe read produced a proposal, so every later worksheet specimen remained
  stopped.
- A deterministic broker regression reproduced the exact structured
  `/usr/bin/uname -s` payload as `pending_action`. The provider schema already
  accepts that payload, while terminal policy omitted both the exact executable
  identity and its narrow safe argument form. The candidate repair registers
  only `/usr/bin/uname` on Ubuntu and auto-authorizes only `-s`; broader
  `uname -a` remains approval-gated. Focused terminal-policy and orchestration
  coverage passes (`49 passed`); the expanded terminal, permission, gateway,
  Ubuntu-worker, Windows-worker, and Ticket 32 matrix passes (`170 passed, 1
  skipped`). Ruff, format, compilation, and diff checks pass.
- The source repair is committed as `f609dc4`; artifact-pin commit `fcbd736`
  locks that exact revision and source SHA-256 `ee7748f81da3`. The single final
  full-suite run completed with `804 passed, 2 skipped, 4 failed` in 742.99
  seconds. All four failures were the expected stale application-source pin
  after the source edit; after pinning the exact commit, those four tests passed
  together (`4 passed, 56 deselected`). The full suite was not repeated.
- No production release, service, configuration, network, credential, OpenWA,
  or WhatsApp state changed during the local repair. Ticket 32 remains
  `ready-for-human` pending an authorized replacement cutover and a fresh
  supervised retry.

### Authorized safe-read policy replacement cutover — 2026-08-24

- The synchronized `711ba88` Git archive was installed as the immutable
  root-owned release
  `/opt/jarvis/releases/711ba886d2ad5e12a0a60f1d963d51730aa9ecb0`.
  Its canonical artifact-lock SHA-256 is `295b9a989b2e`; the installed
  application revision is `f609dc4a4140c802988d7e2b705575a898bfdc65`.
  Locked dependency installation, candidate Compose parsing, all 13 image
  builds, and image-internal bundle verification passed before replacement.
- The verified pre-change backup is
  `/var/backups/jarvis/20260824T111201.322611Z-pre-change`. All 13 candidate
  services then became healthy within their reviewed cold-start windows with
  zero restarts and no OOM flags. Administrative status reported both workers
  ready, messaging ready, audit writable, backup current, and resource pressure
  `ok`.
- The active pointer now targets `711ba88`; `previous` targets rollback release
  `3d07bab`. The root-owned active override SHA-256 is `c570e0d07469`, the
  preserved pending override remains `5f3079be2bbc`, and the exact running
  13-image map was installed root-only at SHA-256 `f424ae62637e`. The verified
  post-cutover baseline backup is
  `/var/backups/jarvis/20260824T112226.205176Z-nightly`.
- Three settled resource samples passed. The proxies remained between 34.9 and
  36.2 MiB under their 48 MiB limits; host memory, swap, and disk headroom
  remained acceptable. The kernel OOM gate passed. Read-only filesystems,
  dropped capabilities, no-new-privileges, non-root identities, restart policy,
  exact published ports, reviewed network membership, and the enabled/active
  backup timer all passed.
- OpenWA was not recreated, modified, or re-paired. It retained container ID
  `24fb501ab8eb`, its original start time, healthy state, zero restarts, no OOM,
  and one named session in `ready`. The Funnel remains enabled at the exact
  `/callback` route to `http://127.0.0.1:8080/callback`.
- Local and Ubuntu cutover transfer artifacts were removed after final
  reconciliation. No WhatsApp acceptance message or terminal action was sent
  during the cutover. Ticket 32 remains `ready-for-human`; the repaired safe
  read and each later worksheet message still require fresh action-time
  confirmation.

### Post-policy-cutover safe-read stop — 2026-08-24

- Fresh supervised controls opened session `S-075` idle with no pending action
  and zero command permissions. The session model was set explicitly to
  `gpt-5.6-luna` with medium reasoning; the persistent default was unchanged.
  `/permissions` and `/revoke session` both confirmed that no command permission
  was active.
- The single confirmed host-neutral safe-read request was admitted as
  `request-a39b5fd42b54449b98a96296ead23b48`. Jarvis returned
  `orchestration-failure-de32c3bc55534841b902a782b2cedca9`, explicitly stated
  that no action was taken, and did not present a proposal or terminal result.
  The request was not retried and every later worksheet row remained stopped.
- Protected trace metadata records one failed model span with the generic cause
  `model returned a malformed action proposal`. Its retained remote exception
  metadata has `operation_started=false`, `may_have_dispatched=false`, and
  `may_have_sent=false`; no structured output or field-level validation cause
  was retained. A further repair cannot be derived safely from this trace.
- Post-stop administrative status still reports application revision
  `f609dc4a4140c802988d7e2b705575a898bfdc65`, both workers ready, messaging
  ready, audit writable, and resource pressure `ok`; all 13 Jarvis services
  remain healthy with zero restarts or OOMs. Ticket 32 remains
  `ready-for-human` at Gate 05 pending containment `/status` and a separately
  justified repair.
