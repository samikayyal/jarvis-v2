Type: task
Status: complete
Blocked by: 10

## Question

Run the focused automated acceptance contract for OpenWA admission and reply behavior, slash commands, busy and modal approval states, direct Responses tool iteration, token-limit termination, deterministic chunking, verbatim trace rotation and failure warning, vault containment, Ubuntu policy and execution, Windows SSH behavior, restart loss, deduplication retention, and limits.

Repair only replacement defects found here. Do not run the repository-wide suite yet and do not activate or remove either runtime.

## Answer

Ran the complete focused replacement-runtime acceptance contract through the
public `tests/personal_runtime` seam. All 139 runnable tests passed. The one
skip is the explicitly POSIX-only process-group containment test, which cannot
run on this Windows development host.

The passing contract covers OpenWA admission and ordered one-attempt replies,
deterministic slash commands, busy and modal approval handling, the direct
stateless Responses tool loop, context-limit termination, deterministic reply
chunking, verbatim trace rotation and non-blocking warnings, vault containment,
Ubuntu command policy and execution, Windows OpenSSH behavior, in-memory restart
loss, seven-day on-disk deduplication, and configured request, tool, command,
output, and listener limits.

No replacement defect was found, so no runtime or test code was changed. The
repository-wide suite was not run, and neither the replacement nor the legacy
runtime was activated, removed, or otherwise modified.

## Comments
