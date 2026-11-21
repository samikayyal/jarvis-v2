# Background browser reader release on Ubuntu

## Deployment inputs

On 2026-11-21, GitHub `main` returned the exact candidate
`2a350f406f653fa359796184a14f81c1a178371c` before packaging. Ubuntu verified the
Git archive SHA-256 as `cbcd9b00a96f4552edbe6016b1b1c2db1b4a17557c5b20df2366dcb3fe1d8d81`.
The service account checked every archived file and all 31 installed package
files in the inactive release. The rollback release was
`fe70273c5551b2214443d1812365bfa160bc86aa`.

Ruff lint and formatting passed across source and tests. Compilation passed.
The full runtime type check reported 46 diagnostics, the same count recorded
for the rollback release. The initial focused test run had three passes and
one timeout while waiting for the fixture's first model call, before release.
The unchanged rerun passed all four release-related tests in 52.16 seconds.
The full Ubuntu run passed 586 tests in 610.18 seconds with one skip for the
opt-in billed screenshot test.

The service started on the verified candidate at 20:56:40 Amman time. Post-start
verification matched the archive and installed package again, with the unit
enabled and running, zero automatic restarts, gateway HTTP 200 and the configured
named WhatsApp session ready. Configuration and prompt hashes matched the
rollback deployment. Systemd still warned about the world-writable unit file,
changes on disk and obsolete CPUAccounting settings. A fresh working session
was acknowledged in the signed-in operator's Jarvis chat.

## Measurement method

[The controlled probe](measure_release.py) starts a real Chromium reader against
the repository's local page fixture. A local stand-in for the model signals that
the reader has reached its first model call and waits for cancellation. The
timer starts just before invoking `release_browser_reader` and records both
the time to the tool's return and the elapsed time through request cleanup.
Each trial verifies that the released ID expired, the Playwright driver exited,
and the proxy thread stopped. The probe uses the installed package from each
release under the Ubuntu service account. Five pairs alternate which version
runs first. There is no injected shutdown delay or external model call.

The end-to-end baseline is the [November 14 WhatsApp hosting comparison](../parallel-tool-calls/deployment-verification-02.md).
That request took 393.361 seconds from admission to runtime completion. Its
single release tool call spanned 99.930 seconds. This comparison uses the
same prompt, a fresh session and temporary full access. The starting comparison
note contains the prior assessment, so its contents differ from the previous
run's input. Before testing, the vault was clean and its local and authenticated
remote Git heads both matched `7b27652211b4e560125919cdee4f368d2b35b0cc`.

## Controlled release timing

| Five trials per release, milliseconds | Previous | Deployed |
| --- | ---: | ---: |
| Median time for release to return | 605.887 | 0.137 |
| Minimum to maximum time to return | 465.589–1049.116 | 0.129–0.163 |
| Median from release through request cleanup | 606.195 | 595.735 |

All ten trials expired the ID and closed the driver and proxy. These
[individual observations](release-timings.jsonl) measure the public tool code
in isolation. Request completion still waits for outstanding cleanup. The
paired trials ended before the live prompt was sent.

## Live WhatsApp result

| Measure | November 14 | November 21 |
| --- | ---: | ---: |
| Admission through runtime completion | 393.361 s | 484.702 s |
| Main model calls | 15 | 12 |
| Reader model calls | 5 | 8 |
| Main model call spans | 109.007 s | 136.928 s |
| Note sync through runtime completion | 87.787 s | 9.193 s |

The [sanitized trace](hosting-benchmark.json) covers the single original request.
Admission was 18:00:28.074342 UTC and completion was 18:08:32.775849 UTC.
The run took 91.340 seconds longer (23.22%) and missed the two-minute target.
The main model requested two releases in one batch after the note synced.
Both results were available 3.696 ms after the model output event, versus
99.930 seconds for the previous run's single release. In this run, one
assignment had already completed and the other had hit its limit. The
controlled trials above separately exercised release of a running assignment.

The largest recorded delay was a 285.010-second terminal execution in a batch
that also read the vault and checked two readers. The command queried Docker
metadata, the systemd cgroup and service properties, and process memory/CPU.
The batch held its other results until completion. One reader completed in
42.446 seconds and the other reached its limit in 229.515 seconds including
cleanup. Those spans overlap the terminal batch. At approximately 18:05 UTC,
the main service thread's kernel stack was in `do_swap_page`; Linux I/O
pressure `full avg10` was 82.87%, versus 0.06% shortly before activation.
This is a host-level sample. The timings do not isolate a particular shell
subcommand, reader, or underlying cause for the entire delay. The benchmark
includes local host inspection, variable public sites, and model decisions.

The 545-character final recommended keeping the laptop and qualified the GCP
regional pricing gap and local OpenWA dependency. The operator's WhatsApp
chat visibly displayed the reply. The transport recorded `unavailable`
with `may_have_sent=true`; exact recipient receipt time is unknown. The
benchmark request and final were each sent once without an operator resend.
The vault reported `synced` at 18:08:23.583142 UTC. Only
`Technical/Jarvis hosting comparison.md` changed, adding a dated assessment.
Local and authenticated remote vault heads matched
`ba64dba2823730c6f9d0ad99cafd35269c41e413` with a clean working tree.

After the run, WhatsApp confirmed full access off, no active request and no
pending action. The gateway was ready, systemd reported zero automatic
restarts, and the service account had only the runtime and its existing
OpenWA node process. No reader browser, driver or proxy process remained.
