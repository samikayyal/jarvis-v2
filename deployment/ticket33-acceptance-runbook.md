# Reboot, resource, recovery, upgrade, and rollback acceptance

This is the human-supervised Ticket 33 worksheet. Run it only after Tickets 31
and 32 are `complete`. Use the exact active release and a root-owned mode-`0600`
evidence file. Do not copy credentials, message bodies, personal identifiers,
terminal output, or diagnostic trace payloads into the ticket.

Stop immediately on `LOGOUT`, a new QR, an identity mismatch, an unknown or
duplicate side effect, an unexpected network member or listener, an unhealthy
restart loop, an OOM, an audit or trace loss, a missing backup, or less than 2
GiB free plus the next 16-MiB trace reservation. Never retry an ambiguous action.

## Gate contract

| Gate | Required evidence | Stop rule |
| --- | --- | --- |
| 01 | Tickets 31 and 32 complete; exact release, application revision, artifact-lock hash, active-configuration hash, image map, 13 healthy Jarvis services, both workers ready, OpenWA container healthy and named session ready, backup current, audit writable, resource pressure `ok`, session idle with no permissions. | Any mismatch stops the run. |
| 02 | The fixed automated workload runs for two continuous hours: 60 bounded reads, 20 multi-turn mixed reads, 8 approvals, 8 rejections, 12 terminal cases, and 12 timeout/unavailability/ambiguous/trace-capacity cases. Samples occur every five seconds and for a ten-minute settling window. | A shortened run, missing request, failed controlled case, or missing sample is not acceptance. |
| 03 | Samples prove every cgroup limit, at most 2.00 aggregate CPU cores, no OOM/restart/PID exhaustion, at most 256 MiB net swap growth, no settling-window swap growth, bounded trace growth without payload loss, reclaimed temporary data, and the protected disk floor. | Do not raise a limit or delete traces to make the row pass. |
| 04 | Isolated low-disk and trace-capacity failpoints block work before model, connector, worker, or outbound dispatch and preserve existing traces. | Never fill the production filesystem or delete a production trace. |
| 05 | A supervised audit-service outage blocks every WhatsApp response and side effect while the local administrative safe read remains available; restoration returns audit writable without replay. | Do not approve an action during the outage; any reply or dispatch stops the run. |
| 06 | A 60-minute real workload contains exactly 20 labeled sequential requests spanning OpenWA status, Gmail/Drive reads, one approved Gmail send, one approved Gmail reply, vault reads and one approved append, Ubuntu safe reads and one approved action, Windows readiness and one bounded action, cancellation, and final status. | Mutations must be reversible and separately approved; no parallel requests or synthetic compression of the hour. |
| 07 | A fresh `pre-change` backup and isolated restore pass checksums, complete database inventory, SQLite integrity, audit readability, ownership/modes, schema, release compatibility, and credential exclusion. | The restore target is new and isolated; never restore over live state. |
| 08 | The pinned upgrade rehearsal uses exact previous/replacement releases, admission-stop time, the fresh backup, and a bounded sanitized OpenWA history export. `--force-failure` restores the compatible previous release and closes unfinished work without replay. | The rehearsal may write only below its new private workspace and may not call Compose, systemd, or OpenWA. |
| 09 | A deliberate live candidate failure triggers the reviewed Jarvis-only rollback: previous release and compatible pre-change state return, all gates pass, and OpenWA retains container identity, start time, pairing, volume, networks, and history without recreation or replay. | Never use `down --volumes`, include OpenWA, pull a floating image, or retry unknown work. |
| 10 | Before reboot, record one nonterminal request, one pending action, and one session permission using reversible labeled specimens; record exact boot ID, release pointers, state counts, OpenWA identity/start time, and image map. | Do not leave an attempted-but-unconfirmed side effect as the specimen. |
| 11 | One full-host reboot returns only the activated pinned release. All 13 services and both workers become healthy/ready within their reviewed windows; OpenWA returns healthy and named-session ready without QR/`LOGOUT`; persistent state survives; the nonterminal request is interrupted, pending action invalidated, session permission revoked, and nothing resumes or replays. | An automatic pull, migration, activation change, repair, or OpenWA recreation fails acceptance. |
| 12 | Fresh post-reboot `/status`, `/permissions`, and one labeled ordinary message pass. Protected reconciliation shows no active request, pending action, open dispatch, outbox row, unresolved attempt, transient scope, duplicate side effect, or recovery-degraded marker. Three settled samples pass. | Physical phone receipt and one gateway acceptance are both required for the final message. |

## Automated endurance

Install the reviewed source and development dependencies into a dedicated
acceptance virtual environment outside `/opt/jarvis/current`. Stop new production
admission for this controlled row, but do not stop OpenWA. Run from the exact
source commit being accepted:

```bash
uv run python -m jarvis_control_plane.ticket33_endurance \
  --source-root "$JARVIS_SOURCE" \
  --python "$JARVIS_ACCEPTANCE_PYTHON" \
  --evidence "$TICKET33_EVIDENCE/endurance.jsonl" \
  --trace-root /var/lib/jarvis/traces \
  --temporary-root /var/lib/jarvis/tmp
```

For a detached host run, invoke the reviewed
`ticket33-endurance-wrapper.sh` through a root-owned transient systemd unit. The
wrapper validates the active Compose model, stops only `inbound_receiver`, and
has a file-parsed exit trap that starts the same container on success, failure,
or interruption. Never replace the wrapper with a nested inline `bash -lc`
command; quoting damage can disable cleanup.

The command refuses acceptance timing other than 7,200 seconds plus 600 seconds
of settling with five-second samples. `--smoke` is only a development check and
is never acceptance evidence. Separately compare initial/final `docker inspect`
restart, OOM, health, image, resource-limit, and start-time fields for all 13
services and OpenWA.

## Degraded-mode rows

Run the low-disk and trace-capacity tests only in their isolated controlled
stores. For the live audit row, first require idle state and no pending action,
then stop only `audit_service`, send one uniquely labeled ordinary message, and
prove no WhatsApp response, model call, connector/worker dispatch, or outbound
attempt. Confirm the local administrative audit-safe read still works. Start
`audit_service`, require audit writable and all services healthy, and reconcile
the retained ingress record as blocked/interrupted without replay.

## Backup, upgrade, and rollback

Create a fresh `pre-change` backup with the installed release command documented
in `README.md`, then restore it once below a new root-owned private rehearsal
directory. Use the exact `upgrade_rehearsal` command from `README.md` with
`--force-failure`, a maintenance-window timestamp, and a bounded content-free
OpenWA history export. Record only hashes, counts, release IDs, and terminal
statuses.

For the live rollback row, activate only an immutable reviewed Jarvis candidate
whose failure is deliberate and occurs before side-effect dispatch. Preserve the
active override, image map, pointers, and pre-change backup before activation.
Rollback must restore the previous compatible state and exact previous images.
OpenWA must not be stopped, recreated, re-paired, migrated, backed up by Jarvis,
or included in either Compose command.

## Full-host reboot

Immediately before reboot, stop admission after the three reversible lifecycle
specimens are durably visible. Capture the exact boot ID and protected baseline.
Use `systemctl reboot` once. Wait through loss of SSH, then reconnect and compare
the new boot ID. Do not run a repair command before observing automatic recovery.
Verify release pointers and images before sending any message.

Complete the post-reboot administrative, durable-state, OpenWA, worker,
exposure, backup-timer, and resource checks. Finally send `/status`,
`/permissions`, and one labeled ordinary WhatsApp message through the existing
operator chat, with action-time confirmation for each send. Require one physical
receipt on the dedicated phone.

Ticket 33 is `complete` only when all twelve gates pass and the ticket contains
a sanitized evidence summary. Otherwise it remains `ready-for-human` with the
failed or blocked row recorded.
