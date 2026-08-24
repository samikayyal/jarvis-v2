Type: grilling
Status: resolved
Blocked by: 01, 02, 03, 04, 05, 06, 07, 08, 09

## Question

Given the resolved runtime, integrations, execution transport, state model, authorization policy, and interaction prototype, what component and trust-boundary architecture prevents sender spoofing, prompt injection, confused-deputy actions, secret disclosure, replay, cross-host privilege expansion, and bypass of approval while remaining operable on the existing Ubuntu laptop?

## Answer

V1 uses a process-isolated reference-monitor architecture on the Ubuntu control
host. The model-driven orchestration agent may interpret requests, retrieve
bounded data, select the natural-language-derived execution host, and create
typed proposals. It is not an authorization authority and cannot directly reach
credentials, state storage, connected-service APIs, the vault Git identity,
OpenWA outbound messaging, or execution workers. A deterministic capability
broker owns all admission, policy, approval, permission, replay, and dispatch
decisions.

The design assumes that model behavior and all retrieved content can be wrong or
hostile. It does not claim that prompting can make the model immune to prompt
injection. Instead, it prevents model or content instructions from acquiring the
authority needed to cross a trust boundary or perform an unapproved side effect.

### Security invariants

These invariants are implementation requirements, not prompting guidance:

1. Only a signature-verified, direct, text message from the one configured
   canonical operator JID can create an operator control.
2. Only the capability broker creates session, request, action, approval, and
   permission IDs or changes their lifecycle state.
3. Model output, connected-source content, quoted-message content,
   and terminal output are never operator identity, approval, policy, or
   permission.
4. The orchestration agent cannot connect directly to a side-effecting
   connector or execution worker.
5. Every connector exposes a closed typed operation set; no connector exposes a
   raw credential, arbitrary HTTP endpoint, arbitrary OAuth scope, generic shell,
   or excluded V1 operation.
6. Approval binds one exact frozen action. Any change of target, operation,
   arguments, content, host, working directory, base revision, or material input
   creates a new action and requires a new policy decision.
7. An approval message is consumed once. Expired, rejected, cancelled,
   invalidated, duplicate, or previously consumed messages cannot authorize work.
8. An exact command permission may satisfy the approval requirement for a
   protected resource, but cannot override a hard prohibition, mandatory-fresh
   rule, host/path/arguments/cwd/compound-structure mismatch, or revocation.
9. Required audit admission must succeed before a side effect is dispatched. An
   audit failure blocks side effects.
10. A selected execution host is frozen before a terminal proposal. An
    unavailable host never silently fails over.
11. A service restart never resumes an interrupted model run, tool call, pending
    action, terminal process, or ambiguous external side effect.
12. Jarvis may prepare and test changes to its own source, but it cannot activate
    a trust-critical Jarvis change. Activation is manual administration outside
    Jarvis.

### Components and trust boundaries

Each component runs under a distinct least-privileged service identity.
Container-to-container calls use authenticated service protocols over
trust-segmented private non-published Docker networks. The native Ubuntu worker
uses a permission-restricted authenticated local socket. The implementation must
preserve the identities and one-way authority relationships below.

| Component | Authority and data | Explicitly denied |
| --- | --- | --- |
| Existing OpenWA messaging gateway | WhatsApp pairing, transport, message persistence, and delivery | Assistant policy, approval state, Google, vault, workers |
| Inbound receiver | Raw-body HMAC verification and defensive decoding of the local OpenWA webhook | Model execution, outbound messaging, connectors, shell |
| Capability broker and state owner | Operator allowlist, atomic inbox, SQLite state, deterministic control grammar, policy, frozen actions, approvals, permissions, cancellation, and dispatch | Raw connector credentials, arbitrary shell, model judgment as authorization |
| Agents SDK orchestration agent | Bounded current context, typed read/proposal tools, model configuration, handoffs, and an OpenAI API credential | Direct state writes, approval parsing, connector credentials, connector sockets, workers |
| Google connector | One configured Google identity, fixed Gmail/Calendar/Drive V1 methods, access-token refresh, and its token record | Arbitrary Google methods/scopes, Drive mutation, destructive Google operations, shell |
| Vault connector | Dedicated clone, configured Markdown paths, repository-scoped SSH identity, exact approved commit/push sequence | Other clones/repos, force-push, history rewrite, automatic conflict resolution, arbitrary filesystem writes |
| Terminal dispatcher | Authenticated worker registry, one frozen terminal action, execution/cancel protocol, bounded output | Host substitution, approval, arbitrary worker registration, connector credentials |
| Ubuntu and Windows workers | One exact host-bound execution request at a time and process-tree cancellation | Jarvis state/policy, other-host identity, general access to service credentials |
| OpenWA outbound connector | Reply/send operation only to the admitted operator conversation through the configured OpenWA session | Arbitrary recipient selection, assistant admission, external side effects other than message delivery |
| Audit service | Append-only structured security events and a bounded redacted query view | Raw message/tool content, model reasoning, alteration or deletion of prior records |
| Public OAuth callback | State-bound Google authorization-code callback and content-free result | Jarvis controls, messages, state, approvals, tools, connectors other than the narrow code handoff, workers |

The capability broker is the reference monitor. It is the only component that
can turn a verified operator control or an existing permission into a connector
dispatch. Connectors independently validate their closed operation schemas so a
broker bug cannot turn a typed operation into arbitrary remote access.

### Network topology

- OpenWA calls the inbound receiver over a private, non-published Docker network
  whose only members are the separate OpenWA gateway and inbound receiver
  containers. The assistant webhook has no host, LAN, or internet listener.
- The capability broker, orchestration agent, containerized connectors, and
  audit service communicate over trust-segmented private non-published Docker
  networks. The colocated native Ubuntu worker alone uses the authenticated
  local socket.
- The worker gateway binds only to the private-overlay interface. Each worker
  initiates an outbound TLS 1.3 bidirectional gRPC session and presents its own
  mTLS certificate. Overlay identity does not replace certificate and
  application-level host authorization.
- Administrative status and audit access are local or private-overlay-only.
- The exact registered Google OAuth HTTPS callback is the sole public Jarvis
  endpoint. It accepts only documented callback fields, requires a
  cryptographically random short-lived single-use `state`, hands the code to the
  Google connector, and returns a content-free result.
- No Jarvis control endpoint, shell, gRPC worker port, broker socket, webhook,
  state view, audit view, or connector API is exposed through public DNS, router
  forwarding, or a public tunnel.

### Inbound admission and sender authorization

For each `message.received` callback:

1. The inbound receiver verifies OpenWA's HMAC-SHA256 signature over the exact
   raw request body before parsing it.
2. It requires the configured OpenWA session, `message.received`, a non-empty
   message ID, `fromMe == false`, and a defensively valid envelope.
3. The broker admits only a direct non-group text message whose normalized
   `from` and `chatId` are the same canonical direct-message JID and whose JID
   equals the configured operator JID. Status, group, media, reactions,
   unresolved LID identities, and other unsupported traffic cannot create work.
4. The broker atomically inserts the unique `(OpenWA session ID, WhatsApp
   message ID)` inbox key before returning acceptance. A duplicate is
   acknowledged but creates no second request, approval, or outbound reply.
5. Unauthorized and unsupported traffic produces only the minimal redacted
   audit required by the state contract. It contributes no message body to
   conversation history or model context.
6. The HTTP request ends after durable admission. Model calls, tools, terminal
   work, approval waits, and outbound sends never run inside the webhook request.

Sender display names, contact text, quoted metadata, message bodies, phone-like
strings, and model interpretation never participate in sender authorization.

### Read and untrusted-content flow

An admitted natural-language request creates one active request. The broker
supplies the orchestration agent with bounded context and closed typed read or
proposal tools. Approval-free reads remain allowed under the previously locked
Google, vault, history, and terminal policies, but every connector enforces
operation, source, size, result-count, and timeout bounds and emits redacted
audit metadata.

Every connector result carries source and request provenance and enters only the
request-scoped connected-service cache or request working data. Email, Drive
content, vault notes, earlier conversation excerpts, terminal output, webpages,
and quoted-message text are untrusted source content. Instruction-like text in
those results has no authority and cannot change the tool surface or broker
state.

The orchestration agent may still be influenced into producing a poor answer,
an excessive permitted read, or a suspicious proposal. V1 contains rather than
claims to eliminate that residual risk: read tools are narrow and bounded,
credential-class resources are unavailable, retrieved content is request-scoped,
cross-system side effects require policy/approval, and normal assistant replies
can be sent only to the admitted operator conversation. Requiring approval for
all sensitive reads would be the stronger alternative, but it is outside the
approved V1 action matrix.

### Frozen action and approval flow

The orchestration agent proposes a typed action; it never executes it. The broker
then:

1. Validates that the operation exists in the V1 action matrix.
2. Applies the deterministic terminal or connected-service policy.
3. Canonicalizes the complete target and payload.
4. Assigns a broker-generated action ID and stores the exact frozen payload,
   safe preview, digest, session/request ownership, creation time, and ten-minute
   expiry.
5. Appends the required proposal event to the audit service.
6. Sends the operator the exact preview and only the choices allowed for that
   action class.

While an action is pending, unrelated text cannot create work. The broker's pure
control parser—not the model—matches an authorized whole-message approval or
rejection using ticket 09's grammar. Before dispatch it atomically verifies:

- the approval message is newly admitted and unconsumed;
- the pending action is the sole current action for the session/request;
- the action is unexpired and has not been cancelled, rejected, invalidated, or
  previously dispatched;
- the stored payload still matches its digest;
- any selected permission choice is eligible and creates exactly the displayed
  normalized terminal permission;
- no relevant permission was revoked;
- the connector/worker identity and selected host match the proposal; and
- the required audit event can be appended.

One local transaction consumes the approval, changes the action state, creates
one dispatch/outbox record, and, when selected, creates the exact permission.
The connector receives the stored frozen payload rather than a regenerated model
payload. Completion removes the exact pending payload and retains only the
already defined redacted operational and audit evidence.

Quoted-message metadata never selects an action. V1's one live pending action is
authoritative, so replaying an old `yes`, resending a webhook, quoting an old
proposal, or confirming after expiry cannot authorize execution.

### Connector and confused-deputy controls

Every connector has a separate typed request schema and target allowlist:

- The Google connector can read only the selected Gmail, Calendar, and Drive
  methods; send Gmail and change Calendar only from a broker-approved frozen
  action; and cannot expose Drive writes or destructive Google methods even when
  an OAuth scope could technically permit one.
- The vault connector separates local search from writes. A write requires the
  exact remote base commit, canonical Markdown paths, complete diff, and commit
  metadata. Any dirty state, base change, or non-fast-forward result invalidates
  the proposal and requires manual recovery.
- The outbound connector fixes the OpenWA session and destination to the admitted
  operator conversation. Model output cannot choose another WhatsApp recipient.
- The terminal dispatcher accepts the exact broker-authorized host, executable or
  script path, arguments, canonical cwd, normalized compound structure, material
  stdin, and timeout. It has no general “run whatever the model sent” route.

OAuth consent, connector availability, a model tool call, a
worker response, and possession of an external credential are capabilities or
signals—not Jarvis action approval.

### Credentials and secret disclosure boundary

V1 deliberately favors unattended restart over encryption at rest:

- Static credentials use root-owned, service-specific plaintext files outside
  the repository and ordinary Jarvis state, with mode `0600`. systemd supplies
  only the required file to the consuming service.
- The Google connector uses a private `0700` directory and `0600` plaintext token
  file owned only by its service account because OAuth authorization or
  reconnection may rotate the refresh token. Replacement is atomic and a grant
  for a different Google `sub` is rejected rather than silently replacing the
  configured identity.
- Credential files are excluded from Git, conversation state, durable memory,
  request caches, audit content, diagnostic traces, command lines, model context,
  child-process inheritance, and routine backups. Recovery reprovisions them
  manually.
- Connector results and errors are sanitized before they cross into the broker or
  orchestration agent. Access tokens, refresh tokens, client secrets, API keys,
  webhook secrets, SSH keys, and mTLS private keys never cross that boundary.

This protects credentials from other unprivileged Jarvis components and from
accidental repository/log/model disclosure. It does not protect plaintext files
from root compromise or an attacker able to read the Ubuntu disk; that reduced
offline-theft protection is an explicitly accepted V1 trade-off.

### Cross-host execution boundary

The worker gateway maps each authenticated certificate identity to exactly one
registered execution host. A worker `Hello` must agree with the certificate and
registered host; capabilities are descriptive and cannot expand policy. The
broker freezes the agent-selected host and visible reason before any proposal.

Each worker accepts only a new action ID for its own host, runs one action at a
time under its least-privileged service identity, uses a controlled process scope,
enforces deadline and cancellation, and returns bounded tagged milestones,
stdout/stderr, and one terminal result. The Ubuntu worker uses a local channel;
the Windows worker uses its outbound private-overlay session and a Windows Job
Object. A disconnected or identity-mismatched worker is unavailable, not a
reason to substitute the other host.

Workers do not receive Google, OpenWA, vault, state, approval, or audit
credentials. Worker certificates authorize protocol participation only; the
broker's action policy and approval still govern every execution.

### Audit, restart, and ambiguous outcomes

The audit service owns its append-only store under an identity the broker cannot
use to rewrite or delete prior records. It accepts bounded structured events and
exposes only the deterministic redacted query/export view defined in ticket 06.
If a required append fails, the broker blocks all side effects including every
OpenWA reply, failure, status response, and warning. Safe reads remain available
only through the local administrative interface until audit is restored.

On restart, the broker:

- marks the active request interrupted;
- invalidates and removes any exact pending-action payload;
- revokes session-scoped command permissions while retaining explicit persistent
  permissions;
- does not resume model, connector, or worker operations; and
- reconciles durable inbox and dispatch records before accepting new work.

Reconciliation marks every nonterminal request interrupted, including admitted
work that had not started; closes known-unattempted dispatches without execution;
marks attempted operations without a confirmed result unknown; removes pending
payloads; and fails closed on inconsistent records. Every unfinished request
requires a fresh operator instruction.

Local transactions can prevent approval and inbox replay, but they cannot create
exactly-once behavior across an external API. If Gmail, Calendar, Git push,
OpenWA send, or a worker action may have succeeded but its result was lost, Jarvis
records the outcome as unknown, does not automatically retry the side effect, and
reports the ambiguity for manual reconciliation or a new explicit instruction.

### Trust-critical changes

Activating changes to message admission, sender allowlists, webhook verification,
the capability broker, authorization or approval parsing, audit service,
connector allowlists, credentials/service identities, worker CA/registration,
systemd units, firewall, private-overlay grants, public endpoint configuration,
or the installed Jarvis orchestration runtime is hard-prohibited through Jarvis.

Development tools may inspect, edit, and test development copies. A manual
administrator outside Jarvis reviews and activates any trust-critical change.
This prevents a freshly approved terminal action from rewriting the component
that evaluates later approvals.

### Threat-to-control map

| Threat | Primary controls | Residual boundary |
| --- | --- | --- |
| Sender spoofing | Raw-body HMAC, canonical direct JID allowlist, group/status/LID rejection, local webhook | Compromise of OpenWA pairing or the trusted inbound receiver requires manual recovery |
| Prompt injection | Untrusted-content labeling, model has no authority/credentials, typed tools, broker dispatch | Model may produce a poor answer, bounded excessive read, or suspicious proposal |
| Confused deputy | Closed connector schemas, frozen target/payload, broker-generated IDs, fixed outbound recipient | Connector or broker implementation bugs remain trusted-code risks |
| Secret disclosure | Per-service credential isolation, no generic connectors, sanitization, content-free audit | Root or offline disk access can read accepted plaintext credentials |
| Inbound replay | Atomic unique `(session ID, message ID)` inbox | Manual reconciliation is required after storage corruption |
| Approval replay/bypass | Pure whole-message parser, one pending action, expiry, digest, atomic consume/dispatch, revocation checks | No exactly-once guarantee after ambiguous external execution |
| Cross-host privilege expansion | Private overlay, per-worker mTLS identity, host binding, no failover, least-privileged worker | A compromised worker has the permissions deliberately granted on its own host |
| Audit bypass | Separate append-only identity and fail-closed side-effect gate | Root administration is outside Jarvis's threat boundary |
| Security self-modification | Trust-critical activation is hard-prohibited and manual-only | A malicious manual administrator remains outside the V1 protection boundary |
| Public attack surface | Private non-published container networks, authenticated local socket for the native Ubuntu worker, overlay-only Windows worker/admin access, one narrow OAuth callback | The OAuth callback and its HTTPS front end still require patching, rate limits, and monitoring |

### Ubuntu operability

The design fits the existing low-resource Ubuntu laptop by using local Python
services, Unix sockets, SQLite WAL, systemd supervision, one active request, one
pending action, and one execution per worker. It adds no Kubernetes cluster,
Redis dependency, general message bus, external state database, semantic index,
or local inference model. OpenAI and Google computation remains remote, and
OpenWA remains the already deployed messaging gateway rather than being folded
into the assistant runtime.

## Comments

- Amended on 2026-08-02 to record the authorized operator's reconciliations:
  protected-resource approval may be satisfied by an exact command permission;
  container communication uses private non-published networks while the native
  Ubuntu worker uses a local authenticated socket; audit failure permits no
  WhatsApp delivery; and restart never starts or resumes unfinished work.
- Ticket 11 refined the local OpenWA webhook transport from literal host
  loopback to a two-member private Docker network after Jarvis's separate Docker
  Compose topology was selected. This preserves the original no-LAN/no-public
  exposure invariant without coupling the two Compose project lifecycles.
