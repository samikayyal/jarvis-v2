# Runtime skills implementation

Implemented on 2026-10-05. The development checkout is
`/home/samik/Projects/Jarvis/code/workspace`, branch `codex/runtime-skills`, based
on `275a21ece05e5cfa8d2b9f9deaf9a013997813e7`. Its original Python source matches
the active release `76eec771389ba0b6eb636702a0f42125698a862e` after line-ending
normalization. The live release and runtime prompt have not been changed.

Four packaged skill files under `src/jarvis_personal_runtime/skills/` provide
Obsidian, Google, terminal, and reminder instructions. `SkillTools` exposes a
configured catalog and `load_skill`, then adds instructions and existing schemas
for loaded groups. Loads persist within one working session and reset on a new
session. Existing validation and approval continuations remain in the original
tools. The reduced prompt is `deployment/personal-runtime/SYSTEM.md.example`.

## Validation

- Focused skill and Responses-loop checks: 40 passed on Windows and Ubuntu.
- Full Ubuntu suite with deployment requirements pinned: 359 passed in 33.53s.
- Ruff check and format check, compilation, and all four skill validators passed.
- Built wheel and loaded all four skill resources directly from it.
- Candidate `--check` against the moved live runtime/configuration passed without
  provider calls. No live model, Google, vault write, or WhatsApp acceptance ran.

## Context measurement

Local `o200k_base` estimates using the live configured schemas and an empty
transcript; these exclude the extra loader round trip and growing history.

| State | Input tokens | Exposed tools |
| --- | ---: | ---: |
| Existing prompt and tools | 3,363 | 23 |
| New fresh session | 373 | 1 |
| Obsidian loaded | 1,547 | 5 |
| Google loaded | 2,184 | 15 |
| Terminal loaded | 709 | 2 |
| Reminders loaded | 1,091 | 5 |
| All skills loaded | 4,412 | 24 |

The old system text alone is 499 tokens; the new core text alone is 148. Fresh
session savings are about 89%; sessions using every skill carry more workflow
guidance and lose that saving. Activation must install the new package and short
prompt together, retain the prior release/prompt for rollback, and verify the
running assistant after restart. Restart ends the in-memory working session.
