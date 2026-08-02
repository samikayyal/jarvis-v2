Type: prototype
Status: resolved
Blocked by: 02, 03, 05, 06, 07, 08

## Question

What concrete WhatsApp transcript and deterministic control grammar make requests, milestone updates, host selection, `/new`, `/status`, `/cancel`, `/model`, `/reasoning`, `/config`, natural confirmation and rejection, action expiry, and reusable command-permission choices clear and unambiguous on the text-only V1 surface?

## Answer

The validated interaction uses a deliberately small deterministic control
grammar around ordinary natural-language requests. A runnable in-memory state
prototype is retained at [`prototype-09/`](../prototype-09/); it exposes the
complete working-session, active-request, pending-action, and permission state
after every simulated WhatsApp message without connecting to OpenWA or any
external service.

### Message classes and precedence

For every authorized-operator text message, Jarvis applies this order:

1. A syntactically valid slash command is handled deterministically.
2. If a pending action exists, an exact approval or rejection reply is handled;
   every other non-command message is refused without changing state.
3. If an active request exists, another request is refused rather than queued.
4. Otherwise the message starts one natural-language request.

Slash commands and approval replies must occupy the complete normalized message.
Matching is case-insensitive and collapses surrounding/repeated whitespace, but
does not use fuzzy matching. Quoted-message metadata may help display context but
never changes which request or action a control message addresses: V1's single
pending action is authoritative.

Ordinary request meaning and host selection remain the orchestration agent's
concern. The operator writes naturally and does not need a host command, prefix,
or exact phrase. The agent knows that Ubuntu is the always-on default and that
Windows is the authorized operator's personal laptop. It selects Windows when
the request explicitly or implicitly depends on that laptop, its files, apps,
or environment; otherwise it selects Ubuntu. The selected host and a short
reason become deterministic request state before any terminal proposal or
execution. If that host is unavailable, Jarvis reports the decision and reason
and waits for further instruction. It does not queue or fail over.

### Slash-command grammar

Commands take only the forms below. Extra tokens produce a usage response and
no state change.

| Command | Deterministic effect |
| --- | --- |
| `/new` | Atomically cancel active work, invalidate the pending action, revoke session permissions, end the conversation/working session, and start a clean session using persistent defaults. Durable history, memory, audit, and persistent permissions remain. |
| `/status` | Show the current safe session ID and inactivity boundary, model/reasoning, active request phase and host, pending-action safe summary and expiry, permission count, and Ubuntu/Windows plus connected-service readiness. Never show credentials, cached content, raw tool payloads, or command output. |
| `/cancel` | Cancel the active request and invalidate its pending action immediately. It does not end the working session or revoke permissions. If idle, report that nothing changed. |
| `/model` | Show the current session model and canonical choices. |
| `/model <model>` | Set the idle working session to exactly `gpt-5.6-sol`, `gpt-5.6-terra`, or `gpt-5.6-luna`; persistent default is unchanged. Refuse while a request or approval is active. |
| `/reasoning` | Show the current session reasoning and canonical choices. |
| `/reasoning <level>` | Set the idle working session to exactly `none`, `low`, `medium`, `high`, `xhigh`, or `max`; persistent default is unchanged. Refuse while active. |
| `/config` | Show persistent model/reasoning defaults and the inactivity boundary. |
| `/config model <model>` | Set the persistent model default for future sessions; current session model is unchanged. |
| `/config reasoning <level>` | Set the persistent reasoning default for future sessions; current session reasoning is unchanged. |
| `/config session-minutes <minutes>` | Set the current and future inactivity boundary to exactly `15`, `30`, `60`, `120`, or `240` minutes. |
| `/permissions` | List stable permission ID, lifetime, host, exact normalized command, canonical working directory, creation time, and last-use time. |
| `/revoke <ID\|session\|persistent\|all>` | Revoke matching permissions immediately, before acknowledgement. This remains available during active work so authorization can always be withdrawn. |

Mutating `/config` commands are refused while a request or approval is active.
`/status`, `/cancel`, `/new`, `/permissions`, and `/revoke` remain available;
revoking a permission also prevents a matching approved-but-not-started action
from executing.

### Request and milestone messages

Acceptance always names the stable request ID and says that the agent is
selecting between default Ubuntu and the personal Windows laptop. Before the
first host operation, Jarvis names the selected host and the request-derived
reason. That selection is then frozen into any terminal-action identity and
approval preview. Milestones are event-driven, not timer-driven, and use the
compact shape `<request ID> update: <completed phase>; <next phase>.` Send them
only at material boundaries such as worker connection, source retrieval,
proposal preparation, execution start, and execution completion. The final
message names the request ID and says success, failure, cancellation, or
interruption; it does not imply a side effect succeeded when delivery or
execution is ambiguous.

### Approval grammar and presentation

Every proposal is frozen and begins with its stable action ID and `expires in 10
minutes`. It displays the exact service operation, recipient/calendar event or
vault diff as applicable. A terminal proposal displays host, normalized command,
canonical working directory, material stdin, timeout, and the policy reason.

Choices are numbered so a single digit is always sufficient:

- Exact-action approvals that cannot create a command permission (Gmail,
  Calendar, vault writes, history/memory changes, and other non-terminal
  actions): `1 Allow this time | 4 Reject`.
- Mandatory-fresh terminal actions: `1 Allow this time | 4 Reject`.
- Permission-eligible terminal actions: `1 Allow this time | 2 Allow for this
  session | 3 Allow every time | 4 Reject`.

The exact normalized natural phrases are:

- This time: `yes`, `okay`, `ok`, `allow`, `approve`, `confirm`, `go ahead`, or
  `1`.
- This session: `allow for this session`, `allow this session`, or `2`.
- Persistent: `allow every time`, `always allow`, or `3`.
- Reject: `no`, `reject`, `deny`, `cancel`, `cancel action`, `don't do it`,
  `do not do it`, or `4`.

Selecting `2` or `3` atomically creates the displayed exact command permission
and executes the pending action; there is no second confirmation. If the action
is not permission-eligible, those replies make no change and Jarvis repeats the
available choices. Qualified text such as `yes, but use another directory`,
multiple choices, reactions, and unrelated messages never approve. `/cancel`
has the same no-execution outcome as rejection but also explicitly cancels the
owning request.

On confirmation or rejection, remove the exact pending payload immediately. On
10-minute expiry, end the paused request, report that nothing executed, and
require a new operator request and fresh proposal. A service restart likewise
invalidates the proposal and interrupts the request; no prior confirmation can
be replayed.

### Reference transcript

```text
Operator: /model gpt-5.6-sol
Jarvis: Session model set to gpt-5.6-sol. Persistent default unchanged.

Operator: /reasoning high
Jarvis: Session reasoning set to high. Persistent default unchanged.

Operator: Check the file I downloaded in Chrome on my laptop, then restart the
          local ExampleService if the file looks valid
Jarvis: Accepted R-014. I'm choosing between the default Ubuntu host and your
        personal Windows laptop from the request and host availability.
Jarvis: Running R-014 on your personal Windows laptop. Reason: the request
        depends on Chrome downloads and a local service on that laptop.
        I'll send milestone updates here.

Jarvis: R-014 update: connected to the Windows worker; inspecting the repository.
Jarvis: Approval required for A-008 (expires in 10 minutes).
        Host: Windows
        Command: `Restart-Service -Name ExampleService`
        Working directory: `C:\Services\Example`
        Reason: service change; reusable exact permission is allowed
        1 Allow this time | 2 Allow for this session | 3 Allow every time | 4 Reject

Operator: yes, but only if it is still healthy
Jarvis: That reply did not match an approval choice. Nothing executed.
        Reply with 1, 2, 3, or 4 (or an exact displayed phrase).

Operator: allow for this session
Jarvis: Approved exact action A-008. Created P-003 for this working session.
        Executing now.
Jarvis: R-014 completed successfully. ExampleService restarted on Windows.

Operator: /permissions
Jarvis: P-003: session; Windows; `Restart-Service -Name ExampleService`;
        cwd `C:\Services\Example`; created 14:22; last used 14:22.

Operator: /new
Jarvis: Started S-022 with persistent defaults. Previous work will not resume;
        pending actions are invalid and session permissions were revoked.

Operator: Open the workbook currently on my laptop and inspect its latest sheet
Jarvis: Accepted R-015. I selected your personal Windows laptop because the
        request refers to a local workbook, but it is unavailable. Nothing was
        queued or failed over. I'll wait for further instruction.
```

This prototype validates that one visible state owner, complete-message control
matching, numbered approval choices, agent-owned natural-language host routing
with a visible reason, and compact milestones are sufficient for V1. Production
implementation should preserve the pure control/state-machine boundary while
replacing the prototype shell and simulated routing events with the actual
orchestration agent.

## Comments
