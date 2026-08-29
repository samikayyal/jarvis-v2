Type: task
Status: resolved
Blocked by:

## Question

What exact current modules, tests, deployment assets, OpenWA contracts, and configuration values must be retained, temporarily bypassed, or eventually removed so a fresh `jarvis_personal_runtime` package can coexist with the old assistant until live acceptance without altering OpenWA pairing or production state?

Record a path-level retained/replaced/deferred-removal inventory and identify the narrow reusable OpenWA facts rather than carrying forward the existing broker, persistence, worker, connector, or recovery abstractions.

## Answer

Add the replacement as an independently composed `src/jarvis_personal_runtime/`
package with its own tests and native-service assets. It may coexist in Git with
the old package, but it must not import `jarvis_control_plane`, join its Compose
graph, read or migrate its state, or reuse its broker, persistence, connector,
worker, audit, archive, backup, or recovery abstractions. Keep the complete old
runtime and deployment intact as an immutable rollback release until ticket 12
passes live acceptance and the operator separately authorizes retirement.

Retain OpenWA itself, `docs/openwa/`, the Baileys ADR, all historical `.scratch`
records, and the exact live gateway configuration and pairing state. Reimplement
only the narrow HTTP facts after ticket-04 validation. The independently
verified outbound runbook uses `messages/send-text`, while the legacy controlled
connector uses `messages/reply`; the latter is not a reason to carry the old
connector forward.

Adding any new Python file under `src/` intentionally diverges from the legacy
`deployment/artifacts.lock.json`, whose source digest covers all `src/**/*.py`.
Do not repin the old bundle around the in-progress replacement; validate and
roll back from its immutable installed release, and create replacement-specific
packaging later.

The complete retained, bypassed, deferred-removal, configuration, OpenWA fact,
and cutover inventory is in the [research artifact](../research/inventory-replacement-seam.md).

## Comments

- Claimed and resolved on 2026-08-30 from repository source, tests, deployment
  assets, gateway documentation, and prior OpenWA acceptance evidence. No live
  service, credential, pairing, network, or production state was inspected or
  changed.
