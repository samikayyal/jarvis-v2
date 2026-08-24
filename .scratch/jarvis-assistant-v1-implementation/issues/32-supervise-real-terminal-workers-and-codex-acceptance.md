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
