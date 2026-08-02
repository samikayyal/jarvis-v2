Type: grilling
Status: resolved
Blocked by: 04, 12

## Question

What deterministic command representation, protected resources, hard prohibitions, mandatory-approval classes, broad read allowance, Gemini classification envelope, session and persistent permission schema, rule precedence, revocation flow, and audit evidence govern terminal actions on both execution hosts?

## Answer

V1 terminal authorization is entirely deterministic. Gemini and every other
model-based command classifier are deferred to V2 and have no authorization
role in V1.

### Rule precedence

Evaluate each terminal action in this fixed order; no lower rule overrides a
higher one:

1. A hard prohibition refuses the action.
2. A mandatory-fresh class offers only `Allow this time`.
3. Protected-resource access requires approval unless an exact session or
   persistent permission already matches.
4. An exact matching permission authorizes execution.
5. A provably safe read outside protected resources runs automatically.
6. Every other eligible action requests approval and offers all three choices.

### Exact terminal-action representation

An exact action is identified by the execution host, resolved executable or
script path, complete argument list, canonical working directory, and the full
normalized structure of any compound command, including component order,
operators, pipelines, and redirections. Environment variables are neither
stored nor compared, and commands inherit the worker environment. Executable
and script contents and file metadata are also not compared; a permission
continues to match when the file at an allowed path changes. This is deliberate
path-based standing authority rather than immutable-behavior authorization: a
persistent permission authorizes whatever behavior the permitted executable or
script path and inherited environment produce later. The authorized operator
explicitly accepted that residual escalation risk on 2026-08-02.

Resolve working directories and identifiable filesystem targets to canonical
absolute paths before policy evaluation, following symlinks, junctions, and
Windows reparse points where possible. A destructive target that cannot be
resolved safely is prohibited.

Parse compound commands into ordered atomic actions and authorize every
component before the first starts. A prohibited component refuses the whole
command; a mandatory-fresh component makes the whole proposal this-time-only.
A reusable permission binds the complete normalized chain, not its individual
components. Runtime failure may leave earlier side effects in place, so report
which components started and completed.

### Hard prohibitions

V1 refuses these actions without offering approval:

- Erasing or formatting disks, partitions, system roots, home directories, or
  other broad or unbounded targets.
- Disabling or bypassing Jarvis policy, approval, audit, host authentication,
  firewall, antivirus, or endpoint security.
- Extracting or transmitting passwords, tokens, private keys, browser
  credentials, pairing state, or other credential-class secrets outside the
  approved credential path.
- Installing covert persistence, opening public inbound control access, or
  weakening the private-overlay or mTLS boundary.
- Destructive Git history rewriting or force-pushing.
- Concealing activity, destroying audit evidence, or evading controls.
- Destructive operations whose target depends on unresolved variables, broad
  globs, ambiguous computed paths, or otherwise cannot be resolved safely.
- Deploying, restarting into, or otherwise activating a change to Jarvis's
  installed orchestration runtime, capability broker, policy or approval parser,
  sender allowlist or webhook verification, audit service, connector operation
  allowlists, credential files or service identities, worker CA or host
  registration, service definitions, firewall, private-overlay grants, or public
  endpoint configuration. Jarvis and Codex may inspect, prepare, and test source
  changes in a development workspace; a manual administrator outside Jarvis must
  activate them.

Bounded deletion and ordinary administrative work are not hard-prohibited.

### Mandatory-fresh approval

Only these three allowed classes can never receive a reusable permission:

- Installing, removing, or upgrading system-level software.
- Materially dynamic shell evaluation, including generated scripts, unless the
  fully expanded action can be frozen and displayed before authorization.
- Downloading and immediately executing code.

Every other eligible approval-gated action may offer all three exact-command
choices, including elevation, deletion, service changes, deployments unrelated
to Jarvis's trust-critical components, external transmission, security
configuration outside Jarvis's trust boundary, and protected-resource reads.

### Protected resources and safe reads

Protected resources include credential and key stores; SSH, GPG, browser,
operating-system authentication, and cloud CLI credential data; OpenWA/Baileys
pairing state; Jarvis secret storage; secret-bearing `.env` data; Jarvis audit
storage, deleted conversations, administrative backups, and security-policy
internals; another user's private files; and paths explicitly configured as
protected. Reading them requires approval but may receive a reusable exact
permission.

A read runs automatically only when deterministic parsing proves that it makes
no filesystem, registry, process, service, package, Git, or configuration
mutation; executes no project or downloaded code; uses no elevation or alternate
identity; touches no protected resource; initiates no network communication;
has bounded runtime and output; contains no redirection, write-capable pipeline
stage, dynamic evaluation, or command-invoking option; and uses canonical paths
of known scope. This is based on command semantics and option allowlists, not
executable names. Uncertainty requests approval.

### Approval, permission, and revocation

Each eligible proposal offers `Allow this time`, `Allow for this session`, and
`Allow every time`, except mandatory-fresh actions, which offer only the first.
Selecting a reusable option atomically creates its exact permission and executes
the pending action without a second confirmation. Ordinary confirmation phrases
such as `yes`, `okay`, `allow`, and `go ahead` mean this-time-only; deterministic
session, persistent, rejection, and cancellation phrase groups select their
corresponding outcomes. Ambiguous or qualified replies do not execute or alter
the frozen proposal. Replies apply only to the one pending action and its
existing 10-minute expiry.

`/permissions` lists each stable permission ID, host, normalized command,
working directory, lifetime, creation time, and last-use time. `/revoke <ID>`,
`/revoke session`, `/revoke persistent`, and `/revoke all` take effect
immediately without approval and before acknowledgement. `/new`, session expiry,
and service restart revoke session permissions; persistent permissions survive
until explicitly revoked. Revocation also prevents a matching proposed but
not-yet-started action from executing.

### Execution and evidence

V1 actions are non-interactive: no user-facing TTY, non-secret stdin is frozen
with the proposal, secret input uses the credential path, every action declares
a timeout and output bound, cancellation terminates the process tree, and an
unexpected prompt fails rather than receiving improvised input.

Permanent redacted audit evidence records stable action/request/session/operator/
worker/host IDs, canonical working directory, normalized command, matched policy
rule, permission ID and lifetime, approval outcome, lifecycle timestamps, exit
code, timeout or cancellation, bounded outcome, and output-truncation flags. It
stores neither raw output nor secret values. Full output remains request working
data except for text sent to the operator as conversation history. If the audit
append fails, every WhatsApp delivery and side-effecting action stays blocked.
Only safe reads through the local administrative interface may continue; no
degraded-state warning is delivered through WhatsApp while audit is unavailable.

## Comments

- Amended on 2026-08-02 to make the already selected path-based matching rule
  conceptually honest: `exact` describes the normalized host/path/arguments/cwd/
  compound structure, not executable contents, file metadata, or inherited
  environment values. Tests must prove that structurally identical permissions
  continue to match across those changes.
- Ticket 10 made activation of Jarvis's trust-critical components manual-only.
  Preparing and testing development changes remains allowed, but the assistant
  cannot deploy them even with fresh approval.
