# Portable principles from OpenAI Codex for Jarvis

Research date: 2026-09-01  
Upstream snapshot: [`openai/codex` `2350823caa2bd3c4a6c7ef46deb390425ca7d5e1`](https://github.com/openai/codex/tree/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1)  
Jarvis snapshot: `f29a234811f2e6655b309c898c60d4eff818d5d1`

## Decision

Yes, but the useful transfer is a small set of engineering principles, not
Codex's Rust implementation, app-server, persistent thread store, plugin
system, or multi-agent runtime. Jarvis is intentionally a single-operator,
single-request native service. The best follow-on work is to make its existing
approval, context, and trace contracts more explicit.

## Principles to carry over

### 1. Treat authorization as an evaluated action, not a yes/no flag

Codex parses a command into command segments before policy evaluation, then
distinguishes `Forbidden`, `Prompt`, and `Allow`. The evaluation considers the
approval policy, filesystem sandbox, network permissions, command origin, and
whether every parsed segment was explicitly allowed
([`exec_policy.rs`, lines 315-437](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/exec_policy.rs#L315-L437)).
Unmatched dangerous commands or commands without effective sandbox protection
are never silently allowed
([`exec_policy.rs`, lines 734-816](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/exec_policy.rs#L734-L816)).

Jarvis currently makes this decision inside
[`RunTerminalTool.execute`](../../../src/jarvis_personal_runtime/terminal.py#L212-L268):
it checks a saved prefix or read-only prefix and otherwise creates a
`PendingAction`. That is correct in spirit, but the decision is not a named,
inspectable result.

**Recommendation: adapt this principle in the next terminal-policy change.**
Introduce a small internal evaluation value such as `allow`, `needs_approval`,
or `reject`, with the reason and matched rule. Capture the exact host, command,
working directory, timeout, and policy/rule identity in the approval
continuation. Resume only that captured action. This avoids turning a later
configuration or policy change into permission to run a different action.

Do not import Codex's Guardian reviewer or its large approval matrix. Jarvis's
authorized operator remains the only reviewer.

### 2. Keep policy separate from host enforcement

Codex explicitly models filesystem and network policy separately from the
backend that enforces it: the core documentation says that `SandboxPolicy`
controls the roots and network behavior while Seatbelt, Landlock, bubblewrap,
or the Windows backend enforces the boundary
([`core/README.md`, lines 27-32](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/README.md#L27-L32)).
Its approval evaluator also says that an allowed command can bypass the sandbox
only when every parsed segment is explicitly allowed
([`exec_policy.rs`, lines 423-437](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/exec_policy.rs#L423-L437)).

Jarvis already has this split at deployment level: terminal policy lives in
[`terminal.py`](../../../src/jarvis_personal_runtime/terminal.py), while
systemd hardening lives in
[`jarvis-personal-runtime.service`](../../../deployment/personal-runtime/jarvis-personal-runtime.service).
The Windows path intentionally uses ordinary OpenSSH and therefore has a
different enforcement envelope.

**Recommendation: adopt as a documented invariant.** Approval means that the
operator authorized an exact action; it is not itself a filesystem, network, or
process sandbox. Keep host enforcement visible in the deployment contract and
make future tools declare both their decision policy and their enforcement
boundary.

### 3. Bound every model-visible context item

Codex's contributor contract requires incrementally built history, no unbounded
injected items, hard caps, and no item larger than 10,000 tokens; injected
fragments are structured context types
([`AGENTS.md`, lines 91-100](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/AGENTS.md#L91-L100)).

Jarvis already preserves the transcript incrementally and estimates the full
candidate context before each Responses call
([`responses.py`, lines 364-415](../../../src/jarvis_personal_runtime/responses.py#L364-L415)).
It also sets `store=False`, disables parallel tool calls, and bounds the final
text and tool results
([`responses.py`, lines 416-515](../../../src/jarvis_personal_runtime/responses.py#L416-L515)).
The remaining gap is that an inbound message has no explicit per-item bound,
and the configured 65,536-character result bound is larger than Codex's
10,000-token review threshold.

**Recommendation: adapt selectively.** Add a separately named inbound-message
bound and a model-context-item bound for vault results, terminal results, and
future tools. Preserve the complete trace, but reject or deterministically
truncate an over-budget item before it enters the transcript. Keep the existing
100,000-token whole-context gate as a second, larger boundary. Treat vault and
terminal content as untrusted data in the tool-result envelope; it must never
become authority.

### 4. Give each tool call a balanced lifecycle and stable correlation identity

Codex keeps core-specific mapping out of the tool registry and uses a dedicated
dispatch trace adapter
([`tool_dispatch_trace.rs`, lines 1-23](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/tools/tool_dispatch_trace.rs#L1-L23)).
The registry starts a trace before dispatch and closes it on unsupported,
failed, or completed paths
([`registry.rs`, lines 511-531](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/tools/registry.rs#L511-L531),
[`registry.rs`, lines 703-749](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/tools/registry.rs#L703-L749)).
Approval decisions retain their source—hook, Guardian, or user—and record that
source with the tool decision
([`approvals.rs`, lines 449-460](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/tools/approvals.rs#L449-L460),
[`approvals.rs`, lines 895-906](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/tools/approvals.rs#L895-L906)).

Jarvis has good event coverage and rotating JSONL persistence in
[`trace.py`](../../../src/jarvis_personal_runtime/trace.py#L22-L91), but the
request ID is not propagated through the Responses runner, terminal execution,
and outbound send events. An approval-required path also does not expose one
stable action ID across proposal, choice, execution, and result.

**Recommendation: adopt this directly, with a small Jarvis vocabulary.** Add
`request_id` and `operation_id` to the relevant existing events, record the
decision source (`read_only_prefix`, `saved_permission`, `operator`, or
`rejected`), and ensure every operation ends in exactly one of completed,
failed, cancelled, or uncertain. Keep trace failure non-blocking, as the
current Jarvis contract requires.

### 5. Keep high-touch modules and public APIs small

Codex's repository rules explicitly prefer private modules and explicit exports,
discourage growing its already-large core crate, target modules below roughly
500 lines, and call for a new module when a file exceeds about 800 lines
([`AGENTS.md`, lines 33-53](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/AGENTS.md#L33-L53),
[`AGENTS.md`, lines 72-90](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/AGENTS.md#L72-L90)).
It also avoids boolean or ambiguous parameters when a named enum or method
would make the call site clearer
([`AGENTS.md`, lines 14-20](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/AGENTS.md#L14-L20)).

Jarvis currently has three high-touch modules above 700 lines:
`config.py` (860), `runtime.py` (746), and `responses.py` (713). Its package
`__init__.py` also re-exports adapters and executors that are implementation
details, and the `PreparedTools.resume(..., approved: bool)` protocol uses an
opaque boolean
([`__init__.py`](../../../src/jarvis_personal_runtime/__init__.py#L3-L62),
[`responses.py`](../../../src/jarvis_personal_runtime/responses.py#L105-L168)).

**Recommendation: adopt as a maintenance rule, not as an immediate rewrite.**
When the next feature touches one of these files, split only at a real seam
(for example, Responses HTTP adapter versus transcript runner), narrow the
package's supported exports, and replace the approval boolean with an explicit
approval decision or named operation. Do not perform a broad refactor merely to
match a line-count target.

### 6. Preserve contract-first testing and versioned interfaces where they pay

Codex requires integration coverage for agent-logic changes and prefers whole
object comparisons over scattered field assertions
([`AGENTS.md`, lines 112-123](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/AGENTS.md#L112-L123),
[`AGENTS.md`, lines 212-214](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/AGENTS.md#L212-L214)).
For a richer external interface it uses generated, version-specific schemas
and bounded ingress queues with an explicit overload response
([`app-server/README.md`, lines 20-59](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/app-server/README.md#L20-L59)).

Jarvis already has a focused public replacement-runtime contract and a final
surviving-suite gate. Continue requiring webhook-to-runtime integration tests
for admission, approval, cancellation, and reply changes; assert complete
request/trace/result shapes where practical.

**Recommendation: preserve now, adapt only if Jarvis gains another client.**
The one private OpenWA webhook does not need JSON-RPC or generated schemas. If a
second UI, control client, or remote runtime is introduced, define a versioned
typed protocol and bounded backpressure behavior before adding endpoints.

## Principles to reject for Jarvis's current scope

- **Codex's persistent threads, resumable history, and durable memory.** Jarvis
  deliberately has one memory-only working session; restart discards it.
- **Multi-agent orchestration, background queues, and parallel tool calls.**
  Jarvis's safety and product contract is one active request, one pending action,
  and sequential tool execution.
- **MCP/plugin discovery and arbitrary dynamic tools.** The prepared-tool
  boundary is intentionally small; adding dynamic capabilities would change the
  trust model and require a new architecture decision.
- **Rust/Bazel/Codex app-server machinery.** Those are implementation choices
  for Codex's cross-platform product, not principles Jarvis needs in its native
  Python service.

## Suggested follow-on sequence

1. Define the exact terminal-action evaluation and approval snapshot, including
   decision provenance.
2. Add request/operation correlation and balanced lifecycle events to the
   existing trace, together with explicit context-item bounds.
3. Apply small-seam module and public-API cleanup only when one of those changes
   naturally touches the relevant file.

No production code, tests, configuration, or service state was changed by this
research. The note is an architectural recommendation for a future,
separately authorized change.

## Sources

- [Codex repository at the pinned upstream snapshot](https://github.com/openai/codex/tree/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1)
- [`AGENTS.md`](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/AGENTS.md)
- [`codex-rs/core/README.md`](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/README.md)
- [`codex-rs/core/src/exec_policy.rs`](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/exec_policy.rs)
- [`codex-rs/core/src/command_canonicalization.rs`](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/command_canonicalization.rs)
- [`codex-rs/core/src/tools/approvals.rs`](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/tools/approvals.rs)
- [`codex-rs/core/src/tools/registry.rs`](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/tools/registry.rs)
- [`codex-rs/core/src/tools/tool_dispatch_trace.rs`](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/core/src/tools/tool_dispatch_trace.rs)
- [`codex-rs/app-server/README.md`](https://github.com/openai/codex/blob/2350823caa2bd3c4a6c7ef46deb390425ca7d5e1/codex-rs/app-server/README.md)
