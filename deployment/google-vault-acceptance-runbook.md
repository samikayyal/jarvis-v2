# Gmail, Drive, and knowledge-vault supervised acceptance

This worksheet is for Jarvis v1 after activation. Calendar is not a v1 capability: the baseline grant requests no Calendar scope, the protocol exposes no Calendar operation, orchestration has no Calendar tool or proposal kind, and the broker has no Calendar dispatcher route. No configuration allowlist or acceptance failpoint can target Calendar.

Controlled providers and unit tests are not production proof. Run this worksheet only with the authorized operator and a second reviewer. Agreement to run this worksheet is not approval for any mutation. The operator separately approves each Gmail or vault action only after comparing the complete frozen proposal.

This worksheet does not authorize activation, deployment, active-configuration edits, service reloads, failure injection, transport or container interruption, firewall or proxy changes, retries after an ambiguous side effect, or direct-provider mutation. Those actions require separate authority. Never expose OAuth URLs, credentials, tokens, account content, signing material, provider identifiers, or private remote URLs in the evidence note.

## Gate contract

| Gate | Required real-system evidence | Stop rule |
| --- | --- | --- |
| 01 | Installed revision, artifact lock, active-configuration hash, image map, healthy services, named OpenWA session ready, audit writable, backup current, resource pressure acceptable, idle Jarvis session. | Any identity, health, exposure, audit, backup, or revision mismatch stops the run. |
| 02 | Connected Google identity matches the configured subject. Granted v1 scopes are exactly OpenID, Gmail read-only, Drive read-only, plus Gmail send only when the send row is being exercised. No Calendar scope is requested by Jarvis v1. | Identity mismatch, missing required scope, or a newly requested Calendar/Drive-write/broad Gmail scope stops the run. |
| 03 | Bounded Gmail list/get and Drive list/get/text-export succeed through the deployed route; unsupported binary Drive export is refused without download. | A mock, direct-provider call, unbounded result, literal unavailable result, or binary download is not a pass. |
| 04 | Broker disconnect makes one read unavailable without stale credential use; a new authorization generation restores the same bounded read. | Do not infer reconnect from browser completion alone; require connected-generation evidence. |
| 05 | An altered Gmail approval phrase is rejected before provider dispatch. | Any proposal change, dispatch, or matching sent message stops the row. |
| 06 | One exact operator-owned labeled Gmail send or reply matches the complete frozen proposal and has one terminal acknowledgement. | Do not approve until To, Cc, Bcc, subject, body, MIME type, attachments, source/thread binding when replying, generation, action reference, and digest all match. |
| 07 | Replaying the old exact approval creates no second send and leaves no pending action. | Never create a replacement message solely to exercise replay. |
| 08 | Deterministic synchronized vault read returns bounded complete Markdown content. | Dirty, stale, ambiguous, excluded, non-Markdown, or incomplete reads are not proof. |
| 09 | One exact Markdown append preserves all prior bytes, produces one normal commit and push, matching clean heads, and one terminal Jarvis acknowledgement. | Git heads alone are not an acknowledgement. Do not run manual push, force, reset, rebase, or conflict repair. |
| 10 | A Calendar request is refused without a tool call, proposal, pending action, or provider dispatch. Destructive Gmail requests, Drive mutations, and vault delete/rename/non-Markdown/hidden/config/nested-repository/outside-root/history-rewrite requests are likewise refused without proposal or dispatch. | Any Calendar surface or exposed excluded operation stops the run. |
| 11 | Health remains good and there is no active request, pending action, executable outbox entry, unresolved dispatch attempt, dirty vault clone, duplicate Gmail or vault side effect, or Calendar surface. OAuth grants still contain no Calendar scope. | Any unresolved input keeps Ticket 31 out of `complete`. |

## Preflight

Run from the exact installed release directory using the reviewed active override:

```bash
export JARVIS_RELEASE_DIR=/opt/jarvis/releases/<installed-commit>
export JARVIS_ACTIVE_OVERRIDE=/etc/jarvis/activation.compose.yaml
cd "$JARVIS_RELEASE_DIR"
git rev-parse --verify HEAD
sha256sum deployment/artifacts.lock.json "$JARVIS_ACTIVE_OVERRIDE"
docker compose --file deployment/compose.yaml --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation config --quiet
docker compose --file deployment/compose.yaml --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation ps
docker compose --file deployment/compose.yaml --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation exec --interactive=false -T capability_broker uv run --no-project python -m jarvis_control_plane.service_runtime admin-status
```

Record only sanitized revision, hashes, health/readiness, restart counts, backup freshness, and pressure state.

## Google authorization and reconnect

Baseline and Gmail-send consent use the authenticated broker only:

```bash
docker compose --file deployment/compose.yaml --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation run --rm capability_broker google-authorize --operation-id ticket31-baseline-01
docker compose --file deployment/compose.yaml --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation run --rm capability_broker google-authorize --operation-id ticket31-gmail-send-01 --access gmail-send
```

The single-use URL stays in the reviewer's controlled browser. Baseline consent contains OpenID, Gmail read-only, and Drive read-only. Gmail-send consent adds only Gmail send. A Calendar scope is a v1 hard stop.

For the reconnect row, disconnect through the broker, prove one unavailable read, authorize with a new operation ID, then prove one fresh read:

```bash
docker compose --file deployment/compose.yaml --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation run --rm capability_broker google-disconnect
docker compose --file deployment/compose.yaml --file "$JARVIS_ACTIVE_OVERRIDE" --profile manual-activation run --rm capability_broker google-authorize --operation-id ticket31-reconnect-01
```

## Gmail exact approval and replay

Use an operator-owned destination, a unique subject beginning `[JARVIS T31 ACCEPTANCE UTC ...]`, and a non-sensitive body. The proposal must show the actual To, Cc, Bcc, subject, body, MIME type, absence of attachments, action reference, digest, and connected generation.

For altered approval, answer with a non-exact phrase and require rejection before dispatch. Prepare a fresh exact proposal only after reconciliation. Reply exactly `yes` once. Verify the actual message and one terminal acknowledgement. Replay the old approval and prove the provider count remains one.

## Knowledge vault

Verify the dedicated clone as its service UID without displaying remote URL, note content, or author identity. Require an attached clean branch with equal local and remote-tracking heads before the read.

For the write, choose one ordinary Markdown note in an allowed directory. The proposal must preserve the synchronized base byte-for-byte and append one unique non-sensitive marker line. Require one path, zero removed lines, one added line, the fixed `jarvis:` subject, configured author shape, and normal push. Reply exactly `yes` once. Require Jarvis's terminal acknowledgement, one new matching commit, equal clean heads, and exactly one marker.

Do not run a manual `git push`, destructive worktree reset, history rewrite, conflict resolution, or direct file edit. If the push result is ambiguous, stop for manual recovery; this worksheet never retries it.

## Exclusions and final reconciliation

First set `/model gpt-5.6-luna` and `/reasoning high`. Send one Calendar request and require refusal without a tool call, proposal, pending action, or provider dispatch. Then send prepare-only requests that attempt destructive Gmail operations, Drive writes, and excluded vault operations; each must produce no proposal and no connector dispatch.

Finish with `/status`, protected durable-state inspection, provider counts, vault clean/equal heads, OAuth-scope inspection, service health, restart counts, audit writability, and backup freshness. Reconcile active requests, pending actions, outbox and dispatch records, vault cleanliness, and duplicate Gmail or vault side effects. Ticket 31 may be marked `complete` only when every in-scope gate above passes and no stop condition remains; otherwise its status stays `ready-for-human`.
