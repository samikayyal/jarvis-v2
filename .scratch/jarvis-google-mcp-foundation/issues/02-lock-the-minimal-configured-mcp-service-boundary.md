Type: prototype
Status: resolved
Blocked by: 01

## Question

What exact minimal runtime interface and working prototype let Jarvis expose
manifest-selected operations from a configured remote MCP service as prepared
tools, bind Google operations to one Google connection, and suspend/resume an
exact Gmail or Calendar pending action without importing the
retired broker or changing terminal saved-permission behavior?

Resolve the checked-in manifest shape, server identity and discovery checks,
configuration fields, operation bounds, simple failure normalization, exact
preview and expiry rules, rejection and ambiguous outcomes, and the smallest
interface worth retaining in production.

## Answer

### Decision

The prototype retains one small `ConfiguredMcpService` prepared-tool adapter.
It is created only by an asynchronous `prepare` gate, is bound to at most one
opaque `McpConnection`, and depends on a narrow `McpTransport` with only
`discover` and `call`. It neither imports nor recreates the retired broker.

The checked-in JSON manifest is version 1 and has exactly four top-level
fields: `manifest_version`, `service`, `captured_at`, and `operations`.
`service` freezes the configured service ID, HTTPS endpoint, negotiated
protocol version, and exact `server_info`. Each operation stores the complete
selected upstream discovery object plus a deliberately narrower prepared-tool
name, description, input schema, and `read` or `write` mode. Unknown manifest
fields, duplicate names, incomplete upstream contracts, and unsupported modes
fail closed.

`jarvis.toml` needs only four non-secret values per service when Ticket 03
wires configuration loading: stable service ID, fixed HTTPS endpoint, manifest
path, and result character limit. Credentials and authorization headers do not
cross this interface. The prototype deliberately leaves the live HTTP/OAuth
transport and configuration parser to Ticket 03, avoiding overlap with the
existing runtime configuration work.

Startup discovery must match the manifest's protocol version, server identity,
and every canonical JSON value in each selected tool declaration,
including its name, input schema, descriptions, output schema when present,
and annotations. Extra discovered tools are ignored and receive no authority.
The narrowed prepared input schemas are also enforced at execution, including
required/extra fields, primitive types, enums, string/integer bounds, and array
item caps. Results exceeding the configured character limit are discarded as
`output_too_large`, not partially returned.

Reads execute once against the current connection. Writes freeze a deep copy
of every argument and display the connection label, service ID, upstream
operation, and canonical complete argument JSON. The pending action has no
wall-clock expiry, matching the current domain rule. It expires on disconnect
or replacement of the connection ID, and restart continues to discard all
pending in-memory state. `9` and `/cancel` reject without a remote call; `2` is
silently ignored because Google actions set `allow_save_permission=false`.
The default remains true, so terminal saved permissions are unchanged.

Each approved write continuation is single-use and marks itself resolved
before its one transport call. A transport failure after that boundary becomes
`outcome_ambiguous`; it is never retried. Other normalized outcomes are
`not_connected`, `connection_changed`, `already_resolved`, `unavailable` (or a
transport-supplied bounded kind), `invalid_response`, and
`output_too_large`. Rejection is the separate `{\"rejected\":true}` result.

The working prototype and contract tests are in
`src/jarvis_personal_runtime/mcp.py` and
`tests/personal_runtime/test_mcp.py`. It composes with the existing Responses
and runtime approval interfaces. The predecessor's product blocker remains:
the official hosted Gmail service exposes draft creation but no send/reply, so
this boundary does not claim otherwise or substitute a direct API.

## Comments

- Resolved on 2026-09-02 with a tested local contract prototype only. No Google
  account was authorized and no remote Google operation was called.
