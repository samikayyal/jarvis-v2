# Ticket 02 deployment and measurement

Date: 2026-11-14 (Asia/Amman).
Status: deployed; visible post-activation acceptance and benchmark pending.

## Source and preflight

Runtime candidate: `fe70273c5551b2214443d1812365bfa160bc86aa` on `main`.
`git push origin main` succeeded; `git ls-remote origin refs/heads/main`
returned that exact SHA before the archive was created or transferred.
Archive SHA-256: `092eb31d3d8d754d2c91adaa26bcaa21e28cb9928cb4afe7e72d9f9c241d997e`.
The Ubuntu checksum check passed before extraction into the commit-named release.

The previous active release and rollback target is
`/home/samik/Projects/Jarvis/code/releases/9c2f7d90b867f63cdd536ac0970916ed8143e796`.
Its own interpreter verified all archived source files and 31 installed package
files against `code/readers-continuation.tar.gz`. Service active/running,
`NRestarts=0`; OpenWA container healthy, readiness HTTP 200, exact configured
named session `ready`. Visible WhatsApp `/status` at 17:12 local confirmed
idle, no pending action, full access off, 15 saved permissions, empty context.

Configuration SHA-256:
`d0efea96c32474876fa58449e50ea12306e836fd3ffb0bcadf5113366c87d9ec`.
Prompt SHA-256:
`9beb38f5530a9bf10fe3159576ee016957ca7227622a2530fd22ca172d3d3db1`.
Neither needs modification for the candidate; the batch maximum defaults to five.
The activation helper checks these hashes and verifies candidate and rollback
with each release's own interpreter and archive. Failed activation automatically
restores and checks the previous release. Existing browser stacks are retained.

## Inactive verification

Hash-locked runtime dependencies and a non-editable package were installed into
the new release. Service-account CLI `--check` and byte-for-byte installed-source
verification passed. Tests run from a separate archive extraction with the locked
development environment, existing Playwright cache and explicit Chrome/driver.

- Ruff lint, format (68 files) and compileall passed.
- Typecheck reports 46 existing diagnostics, matching ticket 01's candidate
  count. This is not a clean typecheck result.
- Initial focused parallel-tools run: 20 passed, one failed in 61.18 seconds.
  `/cancel` reader fixture timed out at `AssignmentModel.arrived()` before the
  scripted model was reached (20-second readiness wait). No cancellation
  assertion failed. This resembles, but does not prove the cause of, the earlier
  fixture readiness failures recorded in ticket 01.
- Unchanged focused rerun: **21 passed in 30.95 seconds**. No source, timeout,
  environment, or browser setting was changed between the runs.
- Full Ubuntu suite: **584 passed, 1 skipped in 523.74 seconds**, exit 0. The initial staging exit remains 1; activation requires
  the separately recorded focused rerun and full-suite exit codes to be zero.

## Planned live evidence

Controlled requests will use existing saved permission for `free -h` to run
bounded, read-only samples concurrently with exact, one-time-approved sleep/print
commands. They will verify approval delivery before sibling completion, the next
approval while an approved action is running, retained results, execution counts,
and `/cancel` plus `/new` cleanup with a visible proposal. No persistent permission
is added for these fixtures.

The original benchmark prompt is:

> Should I keep hosting you on my Ubuntu laptop or move you to GCP? Save the full result in an Obsidian note. Keep your final WhatsApp summary short.

Starting vault HEAD is `9005e0e1c6701aa1499baab0cf92a12742abcabb`, clean.
`Technical/Jarvis hosting comparison.md` is unchanged since the previous accepted
run, SHA-256 `f5eadd168a0a223888bf92149d1221ac52aea2a0bc08ed94be96d5a7655b1c98`.
Preserve its dated history. Compare the measured run against 507.389 seconds,
28 main calls and 8 reader calls. Record approval-mode differences if applicable;
do not silently remove human waiting from admission-to-result time.

`measure-02.py` exports bounded sanitized trace metrics. Terminal overlap uses
actual executor events, not delayed batch results. Reader terminal intervals
include cleanup. Nested/overlapping spans are not summed into elapsed time.
Gateway acknowledgements and timestamps do not establish recipient receipt time.
Raw trace bodies, message identifiers and note contents remain private.

Functional acceptance, benchmark, final cleanup and final evidence review
remain pending; no completed ticket or latency improvement is claimed here.

## Activation and procedure review

Activation completed with exit code 0 on 2026-11-14. The active symlink resolves
exactly to `releases/fe70273c5551b2214443d1812365bfa160bc86aa`. Post-start
installed-source verification passed, the configured session was ready with
HTTP 200 gateway readiness, and the service was enabled/active/running with
`NRestarts=0`. Configuration and prompt were unchanged. The prior release remains
available as the verified rollback target.

Operational commits `b8b479c` and `b40ddcb` were pushed; GitHub main was checked
as `b40ddcbc9f5591887cd241313fe4160d2cbe5a49`, containing the immutable runtime
candidate, before activation. The existing systemd world-writable-unit warning
and unrelated obsolete CPUAccounting warnings were recorded, not changed.

Independent Standards and Spec procedure reviews each found one issue in the
measurement helper: raw control/reason text could escape sanitization, and
identical concurrent terminal commands could receive guessed FIFO attribution.
The helper now allowlists exact controls, omits reader exception reasons, and
marks repeated overlapping command attribution ambiguous. Both reviewers
confirmed their findings resolved. No runtime source changed after the full suite.

The first post-activation UI attempt stopped because Computer Use could not
confidently determine Chrome's URL. The operator requested the in-app browser;
WhatsApp opened there but redirected to a logged-out page. No post-activation
ordinary request, concurrency acceptance, or benchmark is claimed yet.