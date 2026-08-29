Type: task
Status: open
Blocked by: 03, 05

## Question

Implement `run_terminal` for the native Ubuntu host with deterministic simple-command screening, configurable read-only prefixes, host-plus-literal-prefix saved permissions, exact proposal display, modal `1`/`2`/`9` handling, configured working directory and timeout, bounded output, and best-effort cancellation.

Focused tests must prove that compound shell syntax never auto-runs, unmatched mutations pause indefinitely, option `2` persists the displayed rule, rejection returns a tool result, and terminal work is never automatically retried.

## Comments
