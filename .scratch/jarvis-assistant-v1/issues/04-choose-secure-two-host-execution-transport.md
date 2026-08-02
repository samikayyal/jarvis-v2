Type: research
Status: resolved
Blocked by:

## Question

What authenticated transport and worker topology should let the Ubuntu orchestrator address the always-on Ubuntu execution host and the authorized operator's intermittently available personal Windows laptop, while preserving agent-selected request routing, availability reporting, command cancellation, output streaming, least privilege, and no public inbound control endpoint?

## Answer

Use an outbound worker topology over a private authenticated overlay: each worker initiates a mutually authenticated TLS 1.3 bidirectional gRPC `WorkerSession` to the Ubuntu orchestrator, with a Unix-domain/loopback path for a colocated Ubuntu worker. Prefer Tailscale grants for the V1 private overlay, with directly managed WireGuard as the self-managed alternative. Keep mTLS and application-level worker authorization in addition to overlay identity. Define explicit hello/status/heartbeat, execute, milestone/stdout/stderr, cancel, and terminal-result messages; enforce one active execution, agent-owned routing from the natural-language request with Ubuntu as the default and the Windows host identified as the authorized operator's personal laptop, no failover from an unavailable selected host, process-tree cancellation, and overlay-only listener binding. See the [secure two-host execution transport research artifact](../research/secure-two-host-execution-transport.md).

## Comments
