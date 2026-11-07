# Ticket 04 deployment and live acceptance

Date: 2026-11-07 (Asia/Amman).
Status: candidate staged; not yet activated; live acceptance pending.

## Exact source and rollback

Candidate: `551ed852b16f17f87e149feae1690da39114fc5b` on `main`.
`git push origin main` succeeded; `git ls-remote origin refs/heads/main`
returned that exact SHA before staging. The source archive SHA-256 is
`f1f5c0489be344924da8855e206a8cba5a86116e25262e5fc5c11e144fc29213`,
verified after transfer. All archived files and all 31 installed Python/skill
files match it byte for byte; the retired `page_reader.py` is absent.

Staged path: `/home/samik/Projects/Jarvis/code/releases/551ed852b16f17f87e149feae1690da39114fc5b`.
Previous/rollback: `/home/samik/Projects/Jarvis/code/releases/39765480870245a87abc6ee5d9d689af5d6f8aee`.
Before activation the previous service was active/running with zero restarts,
OpenWA was healthy, its exact configured named session `jarvis` was ready,
and visible `/status` confirmed idle, no pending action and full access off.

## Ubuntu provisioning

Ubuntu 26.04 LTS x86-64; Python 3.14.4. Installed the hash-locked deployment
requirements and non-editable package from the verified archive. A resumed
`uv venv` initially attempted to remove the existing environment and failed
on generated files. That inactive environment was retained as `.venv.incomplete-04`
and replaced with a fresh locked installation before the successful probes.
The staging helper now preserves an existing environment.

Playwright 1.58.0 predates Ubuntu 26.04 and rejects automatic provisioning for
that platform. Explicit `PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64`
selects its vendor Chromium 145.0.7632.6 / revision 1208, headless-shell 1208
and FFmpeg 1011 artifacts. `install-deps chromium` installed their Ubuntu
libraries/fonts. This is verified host compatibility, not a claim that
Playwright 1.58 officially supports Ubuntu 26.04.

The browser cache belongs to the service account under
`other/runtime/.cache/ms-playwright`. Tests point to that same cache.
Initial service-account startup failed because Ubuntu restricted anonymous
user namespaces. The committed AppArmor profile grants `userns` only to the
two exact revision-1208 binary paths, following Ubuntu's existing Chrome
profile pattern. The global `kernel.apparmor_restrict_unprivileged_userns=1`
and Chromium sandbox remain enabled. No OpenWA recreation or firewall change
was made.

Installed-package public-browser probe as UID 997: IANA initial reading,
retained findings, fresh browser follow-up and browser/profile cleanup pass.
The independent Selenium Google search returned `unavailable` after the
probe's 20-second default operation timeout; no successful search claim is made.
The live runtime keeps its existing 60-second search-operation timeout.

Authorized reuse of the existing key: installed-candidate screenshot probe
using the existing canvas fixture passed with `gpt-6-luna`, low reasoning,
two model turns, **17.60 seconds**, returning `ORCHID 7391` and the fixture URL.
The original billed fixture first returned `running` at its one 30-second
wait and failed its completion assertion. The operational probe polls the
public wait operation within the configured 120-second assignment budget;
no runtime or test-source changes were made to hide that observation.

Configuration check passes without migration: the live `[web_reader]` only
names model and reasoning. Defaults are 3 active assignments, 3 browser
contexts, 8 retained readers, 24 model turns, 120 seconds, 128000 context tokens
and 8000 output tokens. Main remains `gpt-6-luna`, medium reasoning.
Configuration SHA-256:
`9d76db623deef741509923396b36e46c67d5b4ab94ffa8a07a105ebb94f90c59`.
The existing prompt matches the candidate example and needs no replacement;
SHA-256 `9beb38f5530a9bf10fe3159576ee016957ca7227622a2530fd22ca172d3d3db1`.

## Checks and remaining gates

Focused typechecks, Ruff and formatting pass for candidate source and operational
Python helpers. Initial concurrent/browser/configuration run, begun before the
AppArmor fix, reported 105 passed and 8 failed. Seven failures were browser
startup; the three-reader test hit its provider-arrival timeout. The clean
post-provisioning run and full-suite result remain to be recorded.

Activation, visible hosting comparison, saved/synced note, claim-level source
inspection, follow-up/cancellation, final cleanup and measured comparison are
pending. Unit/provisioning checks do not establish end-to-end acceptance or the
under-two-minute target. Baseline: 458 seconds before completion, 22 main and
27 reader model calls. Reader model time is nested within reader wall time.
