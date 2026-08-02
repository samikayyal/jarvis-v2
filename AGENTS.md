## Agent skills

### Issue tracker

Issues and specs live as Markdown files under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The repo uses the default canonical labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo with root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.

### Delegated work

Use the project custom agent `luna-worker` for clearly bounded implementation or verification subtasks. Give it the exact objective, scope, expected output, and validation; do not assign unbounded repo-wide work or overlapping write ownership.
