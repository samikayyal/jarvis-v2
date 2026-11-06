# Full access

Status: complete

## Objective

Let the authorized operator enable `/full-access` so Jarvis can carry out the
actions needed for the operator's requests without individual approval prompts.

## Agreed decisions

Confirmed by the operator on 2026-10-31 with "yes to all":

- Full access covers all available tools, including terminal execution, scripts,
  package installation, file changes/deletions, email sends, calendar changes,
  and vault edits/deletions. The grant authorizes actions serving the operator's
  requests within actual OS permissions and connected accounts.
- The grant lasts for the current working session. `/new`, inactivity expiry,
  and service restart turn it off; the existing working-session definition also
  ends a session at the configured context-token limit.
- `/full-access` enables immediately without an additional confirmation.
  Repeating it leaves full access enabled.
- `/full-access off` disables it.
- `/status` always displays whether full access is enabled.

Confirmed in the second round on 2026-10-31 with "yes to all":

- Existing tool boundaries remain: vault operations use dedicated Obsidian
  tools, Google Drive tools stay read-only, and Jarvis does not edit `.env` or
  `SYSTEM.md`. Terminal execution must not be used to bypass these boundaries.
- If an action is waiting for approval, `/full-access` enables the mode,
  resumes that frozen action after its existing validity checks, and continues
  the original request.
- `/full-access off` takes effect for subsequent actions. An operation already
  started may finish; the next action that normally requires approval pauses
  unless an existing saved permission authorizes it.
- `/cancel` stops the current request and turns full access off. Completed
  effects remain, and a provider or remote operation may already have started.

## Behavioral contract

The runtime owns the grant and evaluates its current value when authorizing
each action, including actions after an approval continuation. Only admitted
operator commands may change it; model output, loaded skills, retrieved
content, and terminal output cannot enable it.

The grant is in memory and belongs to the working session. It neither creates
nor changes saved permissions. Turning it off restores normal authorization,
including existing saved permissions. `/cancel` revokes it even when there is
no active request. Enabling while a request is already running applies to its
subsequent actions; toggling does not restart or duplicate an attempted action.

Exact mode controls must remain usable while an action is pending. Disabling
the mode does not approve or discard an already pending action. Repeated
enable/disable commands are idempotent and acknowledge the resulting state.
Malformed commands show usage and do not change authority.

Automatic authorization covers the current terminal, Google, vault, and
manifest-selected MCP approval paths. Existing argument validation, configured
hosts/accounts, operation manifests, vault revision checks, execution limits,
tracing, and one-attempt semantics continue to apply. The runtime must communicate
the effective mode to the model and report mode changes to the operator.

## Acceptance criteria

- A fresh working session starts with full access off, and `/status` shows it.
- `/full-access` enables immediately, including while a request is active or an
  action is pending; repeating it does not disable access or replay work.
- Each existing approval-gated tool path executes without prompting when full
  access is on, while its validation and execution contracts still apply.
- Enabling while approval is pending resumes the exact valid pending action
  once and continues under the current session policy. Stale or invalid actions
  still fail their existing checks.
- `/full-access off` restores normal approval decisions for subsequent actions,
  including later actions in the same request or continuation.
- `/cancel` revokes the grant and preserves the existing cancellation behavior
  and uncertainty reporting.
- `/new`, inactivity expiry, context-limit reset, and restart discard the grant.
- Normal mode retains its existing prompts and saved-permission behavior;
  toggling does not change the durable permission store.
- Unauthorized messages, tool results, and malformed commands cannot change
  the grant. Mode controls work through the pending-action command route.
- Help, status, runtime instructions, domain docs, and relevant tool guidance
  consistently describe the two modes.

## Implementation approach

The operator confirmed the complete contract and requested the simplest
implementation with minimal code and no unnecessary security/privacy layers.
Use one in-memory session flag and automatically resume the existing prepared
tool approval continuation in the Responses loop. Keep current checks and limits;
add no configuration, persistence, or policy framework. Refresh the model's mode
instruction on each API call, including approval continuations.

## Documentation

`CONTEXT.md`, the README, runtime prompt, and Google/Obsidian skill instructions
describe normal approvals and session full access. Vault results and traces
identify automatic approval without claiming that a preview was shown.

## Delivery state

Implementation committed on 2026-11-05 in `b05ace7` and integrated on `main`
with the completed page-reader branch. This integration did not itself deploy
full access. On 2026-11-06, combined commit
`39765480870245a87abc6ee5d9d689af5d6f8aee` was deployed to Ubuntu.
Core WhatsApp full-access checks passed; remaining UI checks are incomplete.
See [deployment verification](deployment-verification.md) for evidence and limits.

Integration verification on 2026-11-05 (Windows): `uv run pytest -q` passed
with 609 tests and 9 skips. `uv run ruff check src tests`,
`uv run ruff format --check src tests`, and `git diff --cached --check` passed.

Verification on 2026-11-01:

- Full runtime suite: 584 passed, 8 skipped, one failure from the old expectation
  that `/new` is ignored during pending approval. Updated that test for the agreed
  session-reset behavior; it also checks that resetting does not execute the action.
- Final focused rerun: all 26 runtime/full-access tests passed, including that
  corrected case and the final change suppressing stale enable acknowledgements
  when resumed work is cancelled or full access is switched off.
- All four real Git vault edit/delete continuation scenarios passed, covering
  normal approval and automatic full-access approval with accurate result metadata.
- Ruff lint, Ruff formatting, and `git diff --check` passed.

The final application-code change is 99 net lines. It uses one session boolean,
one shared automatic-approval branch, command/status handling, and approval-source
plumbing. Existing unrelated checkout changes were preserved.
