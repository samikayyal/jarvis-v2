# Secure two-host execution transport

Research date: 2026-08-01  
Ticket: [Choose the secure two-host execution transport](../issues/04-choose-secure-two-host-execution-transport.md)

## Decision

Use a private, authenticated overlay for host reachability and an outbound worker topology for both execution hosts. The intermittently available Windows worker should initiate and maintain a long-lived, mutually authenticated TLS 1.3 bidirectional gRPC session to the Ubuntu orchestrator. The Ubuntu execution worker should use the same worker-session protocol; when it is colocated with the orchestrator, its transport should be a Unix-domain socket or loopback-only channel rather than a network listener.

The recommended V1 layering is:

1. **Private overlay:** Tailscale is the preferred V1 implementation of the private overlay, using explicit grants for only the orchestrator-to-worker service path. A directly managed WireGuard overlay is a viable equivalent if avoiding a hosted coordination/relay dependency is more important than operational simplicity.
2. **Service authentication:** mTLS with a dedicated Jarvis worker CA and one certificate identity per worker. The orchestrator must validate the certificate identity against the registered host (`ubuntu` or `windows`), and the worker must validate the orchestrator certificate. The overlay is not the only authorization boundary.
3. **Worker protocol:** a long-lived bidirectional gRPC stream, initiated by the worker. gRPC provides ordered messages in each stream, independent read/write directions, TLS credentials including client certificates, deadlines, and cancellation hooks; Jarvis must add the application-level execution, milestone, heartbeat, and cancellation messages.
4. **OS execution boundary:** the worker runs as a least-privileged service and launches each terminal action in an individually controlled process scope. On Ubuntu, use a dedicated systemd service user and applicable systemd sandboxing. On Windows, use a service account with only required permissions and a Windows Job Object per execution so cancellation terminates the complete child process tree.

This design does not create a public inbound control endpoint. The orchestrator's worker listener must bind only to its private-overlay address (and, for a colocated Ubuntu worker, only to the local socket/loopback path). It must not be shared with the public messaging webhook, published through public DNS, port-forwarded from the internet, or bound broadly to a public interface.

The choice is a recommendation derived from the current requirements, not a claim that Tailscale, gRPC, or a particular certificate-issuance product has already been provisioned. No host connectivity or deployment state was tested for this research.

## Requirements taken as fixed

The repository contract fixes the following behavior:

- Ubuntu is the default execution host.
- Windows is the authorized operator's personal laptop and is selected when the
  orchestration agent infers from the natural-language request that the work
  requires or clearly refers to that laptop.
- An unavailable agent-selected host never silently fails over to the other host.
- V1 permits one active request and no parallel execution.
- Execution must expose availability, milestone/output progress, and cancellation.
- The control plane must not expose a public inbound shell or control endpoint.

These are repository constraints, not transport defaults. See [`CONTEXT.md`](../../../CONTEXT.md), [`map.md`](../map.md), and the ticket question.

## Evidence and option comparison

### Private overlay: reachability and node identity

Tailscale documents that every device has node identity based on device keys, and that access-control grants can be defined from that identity at network and application layers. Its access-control documentation describes a deny-by-default model when a policy is present and permits narrowly scoped source/destination rules. [Tailscale identity](https://tailscale.com/docs/concepts/tailscale-identity) · [Tailscale access control](https://tailscale.com/docs/features/access-control)

Tailscale also documents NAT traversal, direct peer connections, and relayed connections through peer relays or DERP when a direct path is unavailable. Relayed traffic remains WireGuard-encrypted between the devices. This is useful for an intermittently reachable Windows laptop, but it introduces a bounded operational dependency on the overlay's coordination and relay paths. [Device connectivity](https://tailscale.com/docs/reference/device-connectivity) · [DERP servers](https://tailscale.com/docs/reference/derp-servers)

WireGuard itself provides a smaller, self-managed alternative: peers are configured with public/private keys, endpoints, and allowed IPs; its official quick start documents `PersistentKeepalive` for a peer behind NAT or a stateful firewall that needs to receive traffic after an idle period. WireGuard does not by itself provide Jarvis's worker registry, service authorization, host availability semantics, or command protocol. [WireGuard Quick Start](https://www.wireguard.com/quickstart/)

**Assessment:** choose a private overlay as the reachability layer, but do not treat overlay membership as authorization to execute commands. Tailscale is the practical V1 default if its control/relay dependency is acceptable; bare WireGuard is the self-managed alternative. In either case, the worker service should be reachable only on the overlay path and should still require mTLS plus application authorization.

### SSH with a forced worker command

OpenSSH supports public-key authentication and can restrict an authorized key with a forced command, `no-pty`, and restrictions disabling agent, X11, and port forwarding. The forced command receives the originally requested command through `SSH_ORIGINAL_COMMAND`, and the OpenSSH manual explicitly describes this pattern as useful for restricting a key to one operation. [OpenSSH `sshd(8)` authorized-keys format](https://man.openbsd.org/OpenBSD-current/man8/sshd.8) · [OpenSSH `sshd_config(5)`](https://man.openbsd.org/sshd_config)

SSH also provides an encrypted command channel carrying standard input, standard output, and standard error, and its server-alive mechanism can detect an unresponsive encrypted channel. [OpenSSH `ssh(1)`](https://man.openbsd.org/cgi-bin/man.cgi/OpenBSD-current/man1/ssh.1) · [OpenSSH `ssh_config(5)`](https://man.openbsd.org/ssh_config)

Microsoft documents that OpenSSH Server is available on supported Windows versions, but it is a service that must be installed/enabled and its inbound firewall rule must permit the SSH port. Microsoft also calls out blocked firewalls, NAT, and intermittent connectivity as causes of failed SSH communication. [OpenSSH for Windows overview](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-overview) · [OpenSSH through Windows Firewall](https://learn.microsoft.com/en-us/troubleshoot/windows-server/system-management-components/troubleshoot-openssh-windows-firewall-port22)

**Assessment:** SSH is a viable restricted operations path and a reasonable break-glass/bootstrap path. It can carry a Jarvis worker protocol over stdio and can stream output naturally. It is not the preferred primary V1 worker transport because it makes the application own more of the difficult semantics: reconnecting a long-running request, differentiating a closed SSH channel from a completed command, propagating explicit cancellation into the spawned process tree, and reporting a stable worker-ready state between requests. It also requires an inbound SSH service on Windows, albeit only on the private overlay, and creates a separate key/host-key lifecycle from the worker protocol.

If SSH is retained for operations, use a dedicated service account/key, a forced fixed worker command, `restrict`/`no-pty`, no agent/X11/port forwarding, strict host-key verification, and an overlay-only firewall rule. Never use a general administrator login or pass arbitrary shell text as the application protocol.

### Inbound mTLS worker RPC

TLS 1.3 supports certificate-based client authentication when the server sends `CertificateRequest`; the client then proves possession of the private key through the certificate and `CertificateVerify` exchange. RFC 8705 uses the same mutual-TLS model and defines client authentication based on a validated certificate chain or an explicitly registered self-signed certificate. [RFC 8446, TLS 1.3](https://www.rfc-editor.org/rfc/rfc8446.html) · [RFC 8705, OAuth 2.0 Mutual-TLS Client Authentication](https://www.rfc-editor.org/rfc/rfc8705.html)

gRPC has built-in SSL/TLS credentials and optional client certificates for mutual authentication. Its bidirectional streaming RPCs allow both sides to write messages independently, preserve message ordering within each stream, and fit a typed execution protocol. [gRPC authentication](https://grpc.io/docs/guides/auth/) · [gRPC core concepts](https://grpc.io/docs/what-is-grpc/core-concepts/)

**Assessment:** an inbound mTLS worker on each host is technically sound when the private overlay and firewall are stable. It gives good service identity and explicit streaming/cancellation hooks, but the Windows host must expose an inbound listener and be reachable at the time of connection. That is a worse fit for an intermittently available host than a worker that dials out, and it is unnecessary for the colocated Ubuntu path.

### Outbound worker session

In the outbound topology, the worker is the gRPC client and the Ubuntu orchestrator is the gRPC server. The worker connects when its host is available, authenticates with its unique client certificate, registers its host identity/capabilities, and then waits on a bidirectional stream. The orchestrator sends an execution request or cancellation down that existing stream; the worker sends acceptance, heartbeat, milestone, stdout/stderr chunks, and terminal result messages back.

This is an architectural use of the documented gRPC primitives, not a claim that gRPC itself supplies the Jarvis worker protocol. gRPC documents that bidirectional streams permit independent reads and writes, and its cancellation documentation says the server should stop ongoing computation while also warning that the application must coordinate cancellation with its own long-running work. [gRPC core concepts](https://grpc.io/docs/what-is-grpc/core-concepts/) · [gRPC cancellation](https://grpc.io/docs/guides/cancellation/)

**Assessment:** this best fits the Windows availability and exposure constraints. The Windows service has no public inbound control port and does not require inbound NAT traversal. The Ubuntu orchestrator has one private-overlay listener, and worker presence is observable from the authenticated session rather than inferred from a blind port probe. The cost is a small, deliberate worker protocol and certificate lifecycle, which is justified by the required cancellation, streaming, and host-routing semantics.

## Recommended topology and protocol contract

```text
Authorized WhatsApp request
          |
          v
Ubuntu orchestrator / policy + session state
  - chooses exactly one host
  - owns the active-request lease
  - private-overlay-only gRPC listener
  - validates worker mTLS identity
          |
          | WorkerSession (mTLS, bidirectional stream)
          | <--- initiated by worker; reconnects with backoff
          v
Windows worker service (intermittent)
  - outbound-only application connection
  - host identity = windows
  - one execution scope at a time
  - Job Object per terminal action

Ubuntu worker service (always-on)
  - same WorkerSession messages
  - Unix socket/loopback if colocated with orchestrator
  - private overlay if it is a separate machine
  - systemd process scope per terminal action
```

The implementation should define one service, conceptually:

```protobuf
rpc WorkerSession(stream WorkerToOrchestrator)
    returns (stream OrchestratorToWorker);
```

The exact protobuf names are an implementation detail; the following messages and invariants are not:

- `Hello`: protocol version, stable worker ID, declared host (`ubuntu` or `windows`), boot/session ID, capabilities, and certificate identity observed by the server.
- `Status`: `connecting`, `ready`, `busy`, `cancelling`, `degraded`, or `offline` as appropriate; include monotonic heartbeat time and the current `execution_id` when busy.
- `Execute`: a server-issued `request_id` and `execution_id`, selected host, typed terminal action, working directory, bounded environment, timeout/deadline, and output limits. It must represent an action already accepted by the orchestrator's deterministic policy; the transport must not let a model bypass that policy.
- `Accepted`/`Rejected`: the worker acknowledges the execution ID or gives a structured reason such as `busy`, `unsupported_capability`, or `invalid_request`.
- `Milestone`, `StdoutChunk`, and `StderrChunk`: each includes the execution ID, stream name where applicable, sequence number, and timestamp. The sequence number makes reconnect/replay behavior explicit rather than relying on transport ordering alone.
- `Cancel`: execution ID, reason, and cancellation deadline. The worker acknowledges receipt, interrupts the process scope, and emits a terminal cancellation result only after the process scope has ended or the worker has recorded a bounded cleanup failure.
- `Result`: exit status, termination reason, duration, output accounting, and final sequence numbers.
- `Heartbeat`: worker service state, last accepted execution, and a monotonic timestamp. The orchestrator expires `ready` status after a bounded heartbeat/session timeout and reports the last known state.

The worker must reject any second execution while one execution is active. The orchestrator must enforce the same invariant in its request state. A reconnect may reattach only to the same `execution_id` under a still-valid lease; it must not start a duplicate execution. The exact heartbeat interval, reconnect backoff, output chunk size, and lease grace period require implementation testing and are intentionally not invented by this research.

### Availability and routing semantics

`ready` means all of the following are true:

1. The private overlay path is usable.
2. The TLS handshake succeeded and the peer certificate is valid for the expected side.
3. The worker certificate maps to exactly one registered host identity.
4. The worker completed `Hello` and capability validation.
5. The worker reports that it can accept one execution.

Overlay reachability, a listening TCP port, or a successful TLS handshake alone is not enough to report execution readiness. gRPC's standard health API similarly requires the service owner to update `SERVING`/`NOT_SERVING`; Jarvis should use an application `Status`/heartbeat for the worker because the worker is the outbound client and the real question is whether it can accept a terminal action. [gRPC health checking](https://grpc.io/docs/guides/health-checking/)

Routing is agent-owned but its execution boundary is deterministic:

- The orchestration agent interprets the natural-language request using the
  known host descriptions and selects one host with a short reason.
- Select Ubuntu by default when no personal-Windows-laptop dependency or clear
  operator intent requires Windows.
- Explicit operator intent takes precedence over the default.
- Selected host not `ready`: return a structured unavailable result with the
  selection reason and last-known status. Do not dispatch to the other host.
- Worker `busy`: report the single active request state; do not queue or run a second request.

The orchestrator may reconnect to the same requested host after a transient transport failure, but this is not failover. A disconnected execution must be marked `transport_lost` until the worker either reattaches the same execution ID or reports a terminal result. The implementation must choose and test a bounded lease/cleanup policy for the case where the worker cannot reattach; it must not silently rerun the command.

### Cancellation and output

The user-visible cancel operation should send an application `Cancel` message carrying the exact execution ID. gRPC cancellation/deadline propagation is a second safety signal, not the only process-kill mechanism. gRPC explicitly states that the application handler must coordinate cancellation and stop long-running spawned work; a closed RPC alone does not establish that a child process has stopped. [gRPC cancellation](https://grpc.io/docs/guides/cancellation/) · [gRPC deadlines](https://grpc.io/docs/guides/deadlines/)

On Ubuntu, the worker should launch an execution in a process scope that it can terminate as a unit and whose service permissions do not include unnecessary capabilities. Ubuntu's systemd execution documentation describes `User=`, `CapabilityBoundingSet=`, `NoNewPrivileges=`, and filesystem/network sandboxing controls such as `ProtectSystem=`, `ProtectHome=`, `PrivateTmp=`, and `RestrictAddressFamilies=`. Apply only settings compatible with the commands V1 explicitly permits; do not claim sandboxing is complete until the actual command allowlist has been tested. [Ubuntu `systemd.exec(5)`](https://manpages.ubuntu.com/manpages/noble/man5/systemd.exec.5.html)

On Windows, use a dedicated service identity rather than `LocalSystem`; Microsoft's account documentation states that `LocalService` has minimum local privileges and presents anonymous credentials on the network. Each terminal action should have a Windows Job Object. Microsoft documents that child processes created through `CreateProcess` are associated with the job by default and that `TerminateJobObject` terminates all processes in the job. [Microsoft local accounts](https://learn.microsoft.com/en-us/windows/security/identity-protection/access-control/local-accounts) · [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

Milestones and output must be sent as application messages while the process is running, with backpressure and output caps. A completion message must be sent after the process scope is reaped. If the stream fails, the worker must retain enough local execution state to report whether the process is still running, was cancelled, or exited when it reconnects; the orchestrator must not infer success from a transport close.

## Certificate and overlay boundaries

- Keep overlay node credentials and worker mTLS private keys out of the repository and out of messages.
- Register one certificate identity per worker and map it to one host role. Do not accept a certificate merely because it chains to the worker CA.
- Validate both certificate chain and expected identity/SAN, enforce certificate expiry, and provide deliberate rotation/revocation. RFC 8705 documents both validated-CA and explicitly registered self-signed mTLS patterns; V1 should use a small private CA or an equally explicit registration store rather than a broad public CA trust decision. [RFC 8705](https://www.rfc-editor.org/rfc/rfc8705.html)
- Configure the overlay policy for only the orchestrator-to-worker service path. Do not grant the worker general access to the Ubuntu host, the messaging gateway, or unrelated private services.
- Bind the orchestrator worker listener only on the overlay interface. Confirm host firewall rules allow only the worker overlay identity/address and the single worker port. No internet port forwarding, reverse proxy, public DNS record, or public API route is part of this design.
- Keep SSH separate from the application transport. If enabled for maintenance, restrict it as described above and log it as an operator path, not as a way for the assistant to bypass the WorkerSession authorization and request state.

## Confirmed facts versus bounded uncertainty

**Confirmed by primary documentation:** Tailscale provides node identity, policy-based access controls, and direct/relayed private connectivity; WireGuard documents peer keys and NAT keepalive; OpenSSH supports public-key restrictions and forced commands; Windows supports OpenSSH Server as a service; TLS 1.3 and gRPC support mutual authentication; gRPC supports ordered bidirectional streams, deadlines, and cancellation signals; systemd and Windows Job Objects provide relevant least-privilege/process-lifecycle controls.

**Recommendation/inference:** an outbound mTLS bidirectional worker over a private overlay best satisfies the combined intermittent-availability, no-public-inbound, explicit-routing, streaming, and cancellation requirements. This is a design conclusion from the documented primitives and the repository contract.

**Unresolved until implementation verification:** actual Tailscale/WireGuard installation and policy state; whether the Windows laptop can maintain the chosen overlay path while asleep or changing networks; direct versus relayed path performance; certificate issuance/rotation operations; exact gRPC runtime behavior in the chosen Python/Windows packaging; command-specific sandbox exceptions; and the tested heartbeat/lease values. These must be verified during implementation without changing the locked no-failover behavior.

## Primary sources

- [Tailscale identity](https://tailscale.com/docs/concepts/tailscale-identity)
- [Tailscale access control](https://tailscale.com/docs/features/access-control)
- [Tailscale device connectivity](https://tailscale.com/docs/reference/device-connectivity)
- [Tailscale DERP servers](https://tailscale.com/docs/reference/derp-servers)
- [WireGuard Quick Start](https://www.wireguard.com/quickstart/)
- [OpenBSD `sshd(8)`](https://man.openbsd.org/OpenBSD-current/man8/sshd.8)
- [OpenBSD `sshd_config(5)`](https://man.openbsd.org/sshd_config)
- [OpenBSD `ssh(1)`](https://man.openbsd.org/cgi-bin/man.cgi/OpenBSD-current/man1/ssh.1)
- [OpenSSH for Windows overview](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-overview)
- [Microsoft OpenSSH through Windows Firewall](https://learn.microsoft.com/en-us/troubleshoot/windows-server/system-management-components/troubleshoot-openssh-windows-firewall-port22)
- [RFC 8446: TLS 1.3](https://www.rfc-editor.org/rfc/rfc8446.html)
- [RFC 8705: OAuth 2.0 Mutual-TLS Client Authentication](https://www.rfc-editor.org/rfc/rfc8705.html)
- [gRPC authentication](https://grpc.io/docs/guides/auth/)
- [gRPC core concepts](https://grpc.io/docs/what-is-grpc/core-concepts/)
- [gRPC cancellation](https://grpc.io/docs/guides/cancellation/)
- [gRPC deadlines](https://grpc.io/docs/guides/deadlines/)
- [gRPC health checking](https://grpc.io/docs/guides/health-checking/)
- [Ubuntu `systemd.exec(5)`](https://manpages.ubuntu.com/manpages/noble/man5/systemd.exec.5.html)
- [Microsoft local accounts](https://learn.microsoft.com/en-us/windows/security/identity-protection/access-control/local-accounts)
- [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
