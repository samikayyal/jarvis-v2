Type: task
Status: complete
Blocked by: 08

## Question

Extend `run_terminal` to the configured Windows host using ordinary OpenSSH over Tailscale, PowerShell-oriented read-only prefixes, case-insensitive saved-prefix matching, configured identity and working directory, bounded output, timeout, uncertainty reporting, and best-effort cancellation without custom workers, certificates, registration, heartbeat, or host failover.

Prove the transport and policy behavior with controlled tests before any live Windows command is attempted.

## Answer

Extended `run_terminal` to the configured Windows execution host through
ordinary OpenSSH over Tailscale. The runtime now loads the Windows SSH host,
user, identity file, working directory, and PowerShell-oriented read-only
prefixes as one all-or-none configuration. It invokes non-interactive
PowerShell with an encoded command, preserves normal OpenSSH host-key checking,
uses only the configured identity, and shares the existing command deadline and
bounded combined-output contract.

Windows automatic and saved literal-prefix matching is case-insensitive while
compound PowerShell syntax remains approval-gated. SSH timeout, transport loss,
and local cancellation report that the remote effect may be uncertain;
cancellation remains best effort and execution is never automatically retried.
No custom worker, certificate, registration, heartbeat, or host-failover path
was added.

- Controlled fake-transport and policy tests were completed before any live
  Windows command; no live Windows command was attempted in this ticket.
- Personal-runtime verification completed with 126 passed and one Ubuntu-only
  skip. Ruff, formatting, and bytecode compilation were clean.
- The one final full suite completed with 913 passed and 3 skipped. Four
  failures were the expected immutable legacy deployment artifact-lock checks;
  one unrelated Ticket 23 Windows deadline failure passed immediately when
  isolated once.
- Required standards and specification reviews found no remaining issues after
  extracting the shared subprocess capture lifecycle.

## Comments
