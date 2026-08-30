Type: task
Status: complete
Blocked by: 03, 05

## Question

Implement `run_terminal` for the native Ubuntu host with deterministic simple-command screening, configurable read-only prefixes, host-plus-literal-prefix saved permissions, exact proposal display, modal `1`/`2`/`9` handling, configured working directory and timeout, bounded output, and best-effort cancellation.

Focused tests must prove that compound shell syntax never auto-runs, unmatched mutations pause indefinitely, option `2` persists the displayed rule, rejection returns a tool result, and terminal work is never automatically retried.

## Answer

Implemented `run_terminal` for the native Ubuntu execution host with strict
simple-command screening, configured read-only literal prefixes, saved
host-plus-literal-prefix permissions, and exact JSON-escaped approval displays.
Unmatched commands enter the existing indefinite `1`/`2`/`9` modal; option `2`
saves the displayed rule before one execution, while rejection becomes the
original tool call's result so the Responses loop can finish normally.

The local subprocess adapter uses the configured working directory, deadline,
and combined output limit; traces proposals and terminal activity; bounds the
complete serialized tool result; contains the process group on timeout; and
describes cancellation only as local best effort. Terminal commands are issued
once and are never automatically retried.

- Completed on 2026-08-30 with 120 replacement-runtime tests passing and one
  Ubuntu-only process-group test skipped on Windows; Ruff, formatting, and
  bytecode compilation were clean.
- The one final full suite completed with 907 passed and 3 skipped. Four
  surviving failures are the expected immutable legacy artifact-lock checks;
  one unrelated Ticket 25 Windows timing failure passed immediately when
  isolated once.
- Required standards and specification reviews found no remaining issues after
  serialized-output and background-process containment repairs.

## Comments
