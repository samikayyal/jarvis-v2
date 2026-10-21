# Direct terminal access

Status: complete

Expose `run_terminal` immediately instead of through a runtime skill. Move its
guidance into SYSTEM.md and remove terminal from the skill catalog and package.
Preserve configured-host filtering, all command approval rules, and other skills.

Acceptance: first-request and reset visibility; direct read-only execution;
approval/rejection still enforced; focused terminal/skills/package tests and
repository checks; installed release and live prompt verification; read-only
WhatsApp command without a preceding load_skill.

Deployed and live command verified; see evidence.md. A post-command /status
check has not been recorded for this follow-up.
