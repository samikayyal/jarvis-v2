# Web evidence quality verification

Completed scope: agreed points 2–6. Point 1, section-aware extraction, is excluded.
This is local implementation and isolated Ubuntu verification. No production
deployment, service restart, production configuration edit, or WhatsApp message
was performed for this implementation.

## Scope and rationale

| Point | Delivered behavior | Why |
| --- | --- | --- |
| 2 | Access outcomes plus truncation, source length, body-prefix extraction, anchor presence, render readiness and exact stop reasons; metadata survives worker transport and output fitting. | A successful navigation does not establish that the needed section was read. Anchor presence is explicitly not proof that its content appears in the excerpt. |
| 3 | Packaged instructions start with one or two useful pages and adapt; deliberate larger batches remain possible. Exhausted allowances stop execution without another browser call. | Avoid spending the entire allowance before reviewing whether the sources answer the question. |
| 4 | Default and example total evidence allowance raised from 32,000 to 48,000 characters; 8,000 per page and query/page ceilings retained. Only evidence actually returned is charged after final output fitting. | Four full page excerpts no longer consume the entire default allowance; five full excerpts leave room for search evidence. Explicit existing TOML values still override defaults. |
| 5 | Static-first, explicit rendered follow-up for the same canonical URL with bounded visible-text readiness. Separate anonymous worker; shared budgets and another page attempt charged. Blocked/unsupported access cannot be render-retried. | Recover JavaScript-loaded content while preserving public-network controls, isolation and bounded work. |
| 6 | Specific search/page failures, limit reasons, successful evidence and rendered-readiness gaps are retained in reporting. | Prevent generic web-failure messages from hiding usable sources or confusing Google blockage with page extraction. |

## Verification

- Full exact-source Ubuntu personal-runtime suite: **559 passed in 264.16s**, no skips.
  See [verified-native-suite.log](verified-native-suite.log).
- Latest focused Windows suite: **114 passed in 16.91s** (direct command output;
  the older focused-test log is not the latest run).
- Ruff check passed; Ruff format check reported 57 files already formatted;
  compileall passed. Final `git diff --check` passed with line-ending warnings only.
- Native browser tests cover headed/headless delayed JavaScript content, wait
  timeout, missing anchors, static/cookie isolation, private fetch/redirect denial,
  challenge stop, cancellation and process/profile/Xvfb cleanup.
- Scripted Responses integration covers static-to-rendered tool use. Limit/output
  regressions cover retained source identity, exact serialized evidence charging,
  exhausted allowances, and repeated blocked URLs with different fragments.
- 59 source/test/documentation files matched between local and isolated Ubuntu
  checkout; see [verified-source-hashes.json](verified-source-hashes.json).
- Wheel built and packaged modules/skill verified against workspace bytes:
  `dist/jarvis_v2-0.1.0-py3-none-any.whl`.
  SHA256: `87dd94a518a3507286090efb2011969ac57b87f487b9cb4e58667f744d677a2a`.
- Wheel installed non-editably in `/tmp/jarvis-web-quality-QNRuPi/.venv`.
  Import path confirmed under that environment's `lib/python3.13/site-packages`;
  remote wheel hash matches local.

## Public installed-wheel probe

URL: <https://quotes.toscrape.com/js/>. Headed browser, 60-second operation timeout,
48,000-character shared allowance, no model calls or messages.

| Read | Outcome | Text characters | Expected quote present | Readiness |
| --- | --- | ---: | --- | --- |
| Static | ok | 78 | No | not_requested |
| Rendered, waiting for `Albert Einstein` | ok | 1,499 | Yes | matched |

Remaining evidence was 47,922 after static and 46,423 after rendered; remaining
page attempts decreased from 5 to 4 to 3. Both workers closed and the probe
reported `profiles_remaining 0`. See [public-render-result.log](public-render-result.log)
and [public_render_probe.py](public_render_probe.py).

## Practical limits

- Extraction remains a body prefix; a long page may still omit the relevant section.
- Google challenge/CAPTCHA access remains blocked. Rendered mode is not a challenge
  workaround and does not guarantee access to every dynamic site.
- Adaptive source selection is model guidance; hard allowance enforcement is code.
- Production must receive the new package and an explicit configuration update if
  its TOML still sets 32,000. These tests do not establish production activation or
  WhatsApp acceptance.
- Preexisting vault/search changes and unrelated scratch work were preserved.
