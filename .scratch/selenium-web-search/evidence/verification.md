# Ticket 04 delivery evidence — 2026-10-08

The package and isolated test-environment checks are complete. Production was
not changed or enabled. No live Google search succeeded in this environment.
Successful IANA reading, a cited scripted reply, session reuse, and a fresh
recheck are recorded separately from that failed search.

## Artifact and dependency reproducibility

- Base revision: `9b442d609ff4da2b5203818e9f389e767d26ea50`.
- Built from a clean `git archive` of that revision with this ticket's
  `pyproject.toml` and `web_worker.py` changes overlaid. The unrelated working
  changes in `vault_git.py` and `test_vault_delete.py` were excluded from the
  wheel and preserved in the checkout.
- Wheel: `jarvis_v2-0.1.0-py3-none-any.whl`, SHA256
  `6146bdd58616f62a1348e047e71a9d97be7cb10698a082d30256e28a7fd34810`.
- Python 3.13.3; uv 0.6.14. The build used setuptools 80.10.2 from the
  hash-locked verification environment and `uv build --wheel --no-build-isolation`.
- Installed into a fresh non-editable environment using `uv pip install
  --require-hashes -r deployment/personal-runtime/requirements.lock`, then
  `uv pip install --no-deps <wheel>`. The Ubuntu imported package was
  `/opt/venv/lib/python3.13/site-packages/jarvis_personal_runtime`, not source.
- Package runtime dependencies and build backend now use exact versions matching
  `requirements.in`. The existing hash-locked deployment requirements required no
  version changes. Five locally stale transitive packages were aligned to that
  lock before final testing; comparisons reported zero discrepancies.
- `uv.lock` is now tracked, so the documented clean-checkout `uv sync --locked`
  reproduces development/test dependencies. It completed successfully.
- `uv pip check` passed on Windows and Ubuntu. Ubuntu reported 42 compatible
  installed packages including the isolated pytest tooling.
- Installed-package tests loaded the packaged `web/SKILL.md`, tool schemas,
  adapter, worker, and the real Responses/tool loop. No editable source import
  or production credential was used.

## Ubuntu service-identity test environment

Isolated Ubuntu 24.04.5 image, user/group `jarvis-personal-runtime`, UID 10001,
`umask 077`, private working directory, no desktop or credentials. Browser
workers retained their production minimal environment and new process session.
No OpenWA containers, volumes, configuration, or profiles were mounted.

- Chrome: `/usr/bin/google-chrome-stable`, **155.0.8059.39**.
- ChromeDriver: `/usr/local/bin/chromedriver`, **155.0.8059.39**,
  revision `3ff7ac5a9224be9156d7f8703a06e22890aafd34`.
- Test image ID (manifest):
  `sha256:e33e06faa7c70300f9ad24ea2c084107e8a64b5c117671aac5f5f967a084ccf1`.
- Docker was started locally for this work. Test containers used `--rm --init`,
  `--shm-size=512m`, and `--security-opt seccomp=unconfined` to permit nested
  Chromium sandbox namespaces. This removes Docker's extra syscall filter only
  from these disposable test containers; Chromium's own sandbox stayed enabled.
- [Sandbox status](sandbox-status.json) reports namespace sandbox, PID/network
  namespaces and Seccomp-BPF enabled, with “You are adequately sandboxed.”
  The diagnostic used the installed reader and minimal environment, and read
  `chrome://sandbox` only from test code, never through the public page tool.

This verifies the intended service identity, profile permissions, headless
startup, minimal environment, and worker teardown under Ubuntu in isolation.
It is not a native production systemd/AppArmor/egress or phone acceptance test.
The native service unit and production browser provisioning remain unchanged.

## Exact verification results

| Check | Result |
| --- | --- |
| Package regression before dependency fix | 1 failed, 5 passed; broad dependency ranges did not match required pins |
| Package regression after pin fix | 6 passed |
| Early package/web-tools/Responses checks | 81 passed |
| Search/config/skills checks | 107 passed |
| Initial Windows real-browser checks against installed wheel | 29 passed in 27.15 s |
| First complete suite, before discovered proxy fix | 489 passed, 3 skipped in 407.39 s |
| First Ubuntu installed browser run | 30 passed, 1 failed; background deny discarded a public fixture |
| Controlled private-image regression, before fix | 1 failed, 28 deselected; public page incorrectly `blocked` |
| Intermediate fix | 28 passed, 1 failed; private-DNS redirect incorrectly `ok` |
| Corrected Windows browser regression | 29 passed in 23.70 s |
| [Final Ubuntu installed web/browser/Responses tests](ubuntu-web-tests.txt) | **118 passed**, no skips, in 94.32 s |
| [Final complete suite](full-suite.txt), after runtime correction | **492 passed, 3 skipped**, in 337.78 s |
| [Ubuntu POSIX terminal process test](ubuntu-terminal-test.txt) | 1 passed, 22 deselected in 25.32 s |
| [Additional detached-browser/crash-handler cleanup checks](ubuntu-process-tests.txt) | 2 passed in 42.46 s |
| Probe failure/history/recheck regressions | 3 passed; rerun after final probe follow-up addition |
| Ruff lint/format and compileall | Passed |
| `ty` with `.venv/Scripts/python.exe`, web adapter/worker/browser | Passed |
| `git diff --check` | Passed |
| Separate Standards / Spec reviews | No outstanding actionable findings; two probe findings fixed and regressed |

The three Windows suite skips require POSIX: two installed browser process
tests and one terminal process-group test. All three passed separately under
Ubuntu. The expanded process tests compare browser processes before/after each
request, including detached crash handlers, and verify no profile remains.
No timed-out or incomplete run is counted as a pass. Further runs were triggered
by actual defects, code changes, or previously unverified environment checks.

The complete-suite checkout still contained the user's unrelated vault changes;
the Ubuntu wheel-only tests used the clean artifact described above.

## Scenario coverage

| Scenario | Evidence |
| --- | --- |
| Direct URL and rendered text | Real Chromium fixture; hidden text/page JavaScript excluded; live IANA text |
| Public redirects / private literal and private DNS redirects | Real fixture; public redirect succeeds, private destinations never receive a request |
| Private subresource on public page | Real fixture; image denied before connection while usable public text remains |
| Startup, sandbox, profile isolation, completion/cancellation teardown | Real Ubuntu worker tests, private directory modes, no shared cookies/profiles, no active browser descendants/crash handlers |
| Quick result-only, moderate comparison/upgrade, partial evidence | `test_web_responses.py` and `test_web_tools.py`, scripted provider and controlled browser evidence |
| Genuine empty results vs unknown layout, CAPTCHA stop | `test_web_search.py` and browser/Responses tests; controlled evidence, no live CAPTCHA observed |
| Unavailable browser / exhausted counts / approval continuation | Tool and Responses tests; real missing-driver Windows probe returned unavailable and required notice |
| Session reuse, explicit recheck, no late reply | Responses tests plus [live installed transcript probe](live-ubuntu.json); real worker cancellation tests |
| Zero additional search-service spend and credential separation | Existing provider is reused in production; smoke provider is scripted; no model/search API/proxy charges, Workspace or OpenWA state accessed |

## Live observations and limitations

[Final installed runtime observations](live-ubuntu.json) contain three network
tool results: Google query `blocked`, IANA read `ok`, IANA explicit recheck `ok`.
Both follow-ups find supporting page evidence in the actual runtime transcript
and make no new tool/network call. The scripted final answer quotes returned
IANA text and cites `https://www.iana.org/help/example-domains`.

The query was `site:iana.org example domains`. Docker DNS returned `192.0.0.88`
for `www.google.com` and `accounts.google.com`; the public-address policy correctly
rejected this non-public address. The search yielded no evidence and the reply
explicitly said web access was unavailable. No DNS bypass, CAPTCHA solving,
paid proxy, or repeated challenge attempt was used. **Successful live Google
organic-result extraction remains unverified.**

An [earlier final-wheel run](live-ubuntu-earlier-timeout.json) recorded a timeout
on its first IANA read and a successful explicit recheck. It is retained rather
than replaced by the later successful record. Normal operation deadlines and
count limits were unchanged; no fixed response-time promise follows.

Other observed limits:

- Initial image/package downloads were slow; one TLS download failed and was
  retried during provisioning. Runtime searches perform no installation.
- Windows Chrome 154.0.8037.93 with ChromeDriver 154.0.8037.92 passed direct
  fixture tests. A Windows worker smoke attempt exposed the unsupported
  development fallback's inability to kill the entire Chrome tree. Its test-owned
  processes/profile were cleaned up. The delivered live probe requires POSIX;
  Windows worker production support was not added or claimed.
- The probe uses a scripted Responses provider. Its citations are grounded in
  returned text, but this is not a live-model quality assessment or a WhatsApp
  reply. No production host change, enablement, model call, or message occurred.

## Reproduction

Use the deployment guide's clean install, browser provisioning and optional
`verify-web.py --live --output ...` instructions. Copy tests outside the source
tree before invoking the installed environment:

```console
umask 077
JARVIS_TEST_CHROMIUM_BINARY=/usr/bin/google-chrome \
JARVIS_TEST_CHROMEDRIVER_BINARY=/usr/local/bin/chromedriver \
uv run --no-project --python /opt/venv/bin/python python -m pytest \
  /checks/test_web_browser.py /checks/test_web_installed_browser.py \
  /checks/test_web_tools.py /checks/test_web_responses.py \
  /checks/test_web_search.py -q -p no:cacheprovider
```

Fixture routing exists only in test code and temporary worker entrypoints.
There is no runtime private-address exception. Production activation must still
follow the documented validated restart and separately authorized real WhatsApp
search/cited-phone-reply acceptance, with working Google access from that host.
