Type: task
Status: complete
Blocked by: 02

## Question

Implement the fresh native runtime foundation with `.env`, `jarvis.toml`, and `SYSTEM.md` loading; validated defaults; in-memory working sessions; one active request; deterministic slash commands; busy and cancellation behavior; the indefinite modal `1`/`2`/`9` approval state; saved-permission TOML updates; and the seven-day message-ID cache.

Focused tests must establish the state transitions and configuration contract without importing legacy control-plane state or policy machinery.

## Answer

Implemented a fresh `jarvis_personal_runtime` package with one admitted-text
runtime seam and no imports from `jarvis_control_plane`. The foundation loads
and validates `.env`, `jarvis.toml`, and `SYSTEM.md`; keeps working sessions,
the single active request, and the indefinite approval modal in memory; routes
all slash-prefixed input deterministically; cancels foreground work locally;
updates only the saved-permissions TOML section; and claims message IDs in an
atomic replacement-owned seven-day disk cache.

The fixed request-runner interface returns completed, approval-required, or
context-limit steps. This leaves the direct Responses loop, OpenWA admission,
and terminal execution in their later tickets without introducing a broker,
connector framework, worker, recovery store, or legacy compatibility layer.

## Comments

- Completed on 2026-08-30 with 49 focused replacement tests passing, Ruff and
  formatting clean, bytecode compilation passing, and no legacy imports.
- The one final full surviving-suite run completed with 837 passed, 2 skipped,
  and the four expected legacy deployment-pin failures. Ticket 02 deliberately
  records that adding the fresh package makes the mutable checkout differ from
  the immutable legacy `artifacts.lock.json`; that old rollback pin was not
  weakened or repinned.
