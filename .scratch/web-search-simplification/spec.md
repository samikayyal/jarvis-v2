# Simplify public web reading

Status: complete

Implement the recommended first pass: allow anonymous public text on pages with
login widgets; share and narrow page challenge detection; reduce batches from
20 to five URLs; simplify output fitting and failure state. Preserve the public
network proxy, URL/DNS policy, anonymous profiles, Chrome sandbox/download
restrictions, separate static/rendered workers, request allowances, source
identities, extraction metadata, and cancellation cleanup.

Acceptance requires focused and full runtime tests, actual Chrome coverage on
Ubuntu, comparison against active production source, a candidate committed and
verified on GitHub before activation, installed-package/service-account checks,
and visible WhatsApp tests through the existing in-app tab. Check citations,
static/rendered behavior, public text with a login widget, final idle status, and
worker/profile cleanup. Commit and push the deployment evidence afterward.

The single JavaScript-enabled browser alternative was an evaluation option, not
part of the recommended first pass. Keep the existing browser separation.

Initial state: local main d9d660a; active release 9520982fd6fc6f780dfe975b4a6bbe61789b7ff7.
Five preexisting edits under assistant-redesign/terminal-core are unrelated and
must remain untouched and unstaged.

Completed 2026-10-29: deployed `f8f78112d5e1c98473be588709e1bd86c6d7f412` after
GitHub verification; Ubuntu 581 tests passed and visible WhatsApp acceptance,
final idle status and browser cleanup passed. See [deployment evidence](deployment-verification.md).
