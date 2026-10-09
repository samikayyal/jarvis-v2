# Native production deployment and WhatsApp acceptance — 2026-10-09

This is subsequent operational work authorized by the user's request to continue
through the open WhatsApp chat. It does not replace the ticket's earlier isolated
[delivery evidence](verification.md).

## Release and configuration

- Active native release: `25ed2fb6286b8c9aaaa15c513f0e9d3ee42b0b93`.
- Previous release: `f2d882e90daa5cf04647a8f54ab784a0de8b2e88`.
- Local feature source: `44a3417`, native executable selection `075387d`, and
  anonymous-home fix `85193d5`. The remote candidate also preserves the existing
  deployed delete-approval fix from `f2d882e`; local unrelated vault changes were
  not staged or overwritten. The remote candidate's `vault_git.py` matches the
  previous deployed file.
- Release root: `/home/samik/Projects/Jarvis/code/releases/`; `code/current` points
  to the active release above. Build used Python 3.14.4, `uv sync --locked`,
  `uv build --wheel`, hash-locked dependency installation, wheel installation
  without dependency resolution, and service-account CLI `--check`.
- Enabled `[web_search]` with 20-second operation timeout, five results per query,
  8,000 characters per page, 32,000 total evidence characters, quick limits of
  one query/two pages, and moderate limits of three queries/five pages.
- Stopped the service before configuration edits, asserted all existing parsed
  sections were unchanged, validated the candidate, atomically switched the
  release symlink, started it, and checked service/OpenWA readiness. The host's
  relocated config paths required an equivalent transaction rather than the
  generic config editor's original path assumptions.
- Private rollback backup:
  `/home/samik/Projects/Jarvis/other/web-activation-backup/25ed2fb6286b8c9aaaa15c513f0e9d3ee42b0b93/`.
  Restore that configuration and the previous release while the service is
  stopped; validate with the previous CLI, start, and recheck readiness. Native
  Chrome installation is additive; neither OpenWA nor its browser was changed.
- Live config SHA256:
  `d835da06a7d66d3d186d86bcbb51931a5a50c4665f10f74f7cbd3f8de6df3ef2`.
- Existing live `SYSTEM.md` already matched the packaged skill-aware example;
  no prompt change was required. SHA256:
  `eb5ab2cd13457ba4a3b0db2a702c2bb9c7c1ce967e1a84e31335c1c684373247`.

## Native browser prerequisites and defect found

The existing Snap launchers failed under the service identity. Installed the
official native Google Chrome deb and matching ChromeDriver, both
**155.0.8059.39**, at `/usr/bin/google-chrome-stable` and
`/usr/local/bin/chromedriver`. Discovery now prefers these explicit native paths.
No runtime search downloads drivers or installs dependencies.

- Chrome deb SHA256: `c58aa0f2cd66179c9f050e062c882d27aa9b9f8c2b7c73fee3498560b5ed0b38`.
- Driver zip SHA256: `a65f025c692e686ec2a21fa6b9639c69fa9813683a47af9cabf1ba0b1f453e42`.
- Direct service-account startup succeeded, but startup with the worker's minimal
  environment failed with `SessionNotCreatedException: Chrome instance exited`.
  Changing only `HOME` to a fresh temporary directory made startup succeed.
- Fixed the worker to create `HOME` beneath its private request profile, excluding
  inherited credentials and service-account state and sharing profile cleanup.
  The regression failed first with `KeyError: HOME`, then passed with the fix.
- Service-account Chrome reported PID/network namespaces and Seccomp-BPF sandbox
  enabled and “You are adequately sandboxed.” No `--no-sandbox`, global AppArmor
  relaxation, authentication reuse, or paid proxy was introduced.

## Verification

- Native initial candidate: **495 passed in 115.59 seconds**.
- After anonymous-home fix: **495 passed in 94.97 seconds**, including real-browser
  tests with the explicitly provisioned binaries. Ruff and formatting passed.
- Windows focused browser regression: **28 passed, 1 POSIX-only skip**; the
  anonymous-home regression was observed red before the fix. Formatting was
  corrected and checked after the focused run.
- Both Standards and Spec reviews found no code issues. Both identified the same
  stale provisioning warning; the guide now correctly names
  `google-chrome-stable` as preceding `google-chrome` and requires verifying the
  resolved paths. This documentation-only correction followed the deployed build.
- Installed wheel probe ran outside the source tree as service UID 997. Direct
  IANA reads, cited scripted replies, follow-up reuse, explicit reread, and profile
  cleanup passed. See [service-account observations](production-service-account.json).
  The scripted probe makes no model or messaging calls.
- Google returned `blocked` with zero results on the native host. This was not
  browser unavailability after the fix. No challenge bypass or search retry was
  attempted inside either request. Successful live Google results remain
  **unverified**; the structured outcome alone does not prove whether the cause
  was an access challenge or another navigation restriction.

## Real WhatsApp acceptance

Used the existing authorized Jarvis chat in the user's in-app browser. The live
model remained `gpt-6-luna` with medium reasoning. These messages use the existing
configured model service; no additional search service, proxy, or model fallback
was introduced.

| Scenario | Observed result |
| --- | --- |
| Ask for an official IANA lookup | Loaded `web`, made one quick `search_web` call; result `blocked`, zero results. Reply disclosed unavailability and stopped without inventing a citation. |
| Supply `https://www.iana.org/help/example-domains` directly | One `read_web_pages`, outcome `ok`. Reply correctly said documentation use, unavailable registration/transfer, and best-effort HTTP unsuitable for production, with the actual source URL. |
| Ask whether example.org can be registered, using prior evidence only | Correct short answer with the same source citation. No new tool call. |
| Explicitly ask to reread that URL once | One fresh `read_web_pages`, outcome `ok`; cited answer confirmed the registration restriction. |
| Final `/status` | Active request: none; pending action: no; saved permissions: 11. |

[Sanitized tool trace](production-whatsapp-tools.json) contains only this test's
web tool calls/outcomes/source identities; provider payloads and credentials were
not exported. There were exactly four tool calls: one skill load, one search,
and two page reads. No vault, terminal, email, calendar, or mutation tools ran.
The screenshot is retained locally at
`../live/whatsapp-acceptance.jpg`, cropped to the Jarvis conversation.

Final host checks: active release matched; service `active`, `NRestarts=0`;
listener `172.20.0.1:9011`; gateway readiness HTTP 200; exact named session `ready`;
web skill exposed both `search_web` and `read_web_pages`; prompt matched;
service-owned browser/driver/worker process count zero and request profile count
zero. OpenWA deployment, pairing state, and credentials were preserved.

**Operational result:** deployed and enabled. Public page reading and cited
WhatsApp follow-ups/rechecks passed. Google searching is still blocked and must
not be presented as working search. No further production change is needed for
this acceptance record; a future successful Google lookup requires the external
restriction to clear without bypassing it.
