## Agent skills

### Issue tracker

Issues and specs live as Markdown files under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The repo uses the default canonical triage labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. Completed implementation tickets use the separate terminal lifecycle status `complete`. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo with root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.

## Deployment: sync with GitHub

Every Ubuntu deployment must use source committed and pushed to this project's GitHub repository before activation.

- Commit the intended deployment changes, push the deployment branch, and verify that GitHub contains the exact candidate commit before activating it.
- Build and deploy that verified commit. Direct transfers (Git bundles, patches, or archives) are permitted only when the final candidate source matches the verified GitHub commit; Ubuntu-only commits or uncommitted patches must be synchronized first.
- If the push or remote verification fails, keep the candidate staged and leave the active release unchanged. Resolve the sync failure before deployment.
- Record the deployed commit and verification results, then commit and push any resulting deployment documentation so GitHub remains current.
