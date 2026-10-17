# Headed Chrome diagnostic — 2026-10-16

User authorized testing normal headed Chrome with a separate anonymous profile.
Ran one Google query on the production host as service UID 997, using the active
installed release `630811c7ff65868049fd0b1c1c9e05e91daabe93`.

The test supplied a driver factory that removed only `--headless=new` from the
runtime's browser options. Headed Chrome used a private Xvfb display (1280x900),
not the user's physical desktop. A new temporary HOME and profile were used;
no login, existing cookies, OpenWA state, or personal profile was reused.
The existing proxy, sandbox option, scheme restrictions, script policy and
20-second browser operation timeout remained. Chrome and ChromeDriver were both
155.0.8059.39. No production source, release symlink, config, or service restart
was changed.

Query: `site:iana.org example domains` (same as the prior candidate diagnostic).

Observed result:

- Headless option was absent.
- Google navigation returned in 4.34 seconds, with outcome `blocked` and reason
  `Google blocked automated search traffic`.
- Final origin/path: `https://www.google.com/sorry/index`.
- Visible message: “Our systems have detected unusual traffic from your computer
  network. This page checks to see if it's really you sending the requests, and
  not a robot.”
- reCAPTCHA detected; no interaction, solving, bypass, or retry occurred.
- Public-network proxy rejection: false. Organic results: zero.
- Temporary directory removed; zero live members of the test process group.
  A separate process check found no Chrome, ChromeDriver or Xvfb processes.
- Production runtime remained active, NRestarts=0.

Conclusion: headed mode alone did not resolve the observed Google block. This
single test is compared with the previous headless failure, not a simultaneous
A/B test; it cannot attribute the challenge specifically to IP reputation,
Selenium detection, account absence, or other signals. Xvfb supplies a headed
browser environment but does not reproduce an interactive personal desktop.

Raw redacted observations: `../live/headed-diagnostic-result.jsonl`.
Diagnostic source: `../live/headed_diagnostic.py`.
No deployment change is justified by this result.
