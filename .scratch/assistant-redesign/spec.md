# Jarvis assistant redesign

Status: in-progress

Implement the agreed identity/context, initiative, evidence, scope, WhatsApp
presentation, and bounded-failure improvements. Intermediate progress messages
are explicitly excluded by the operator. Keep model, credentials, approval rules,
and live limits unchanged. Use deployed release 5dfac1e as the baseline and preserve
unrelated edits in the original checkout.

Acceptance: focused and full runtime tests; lint/format; installed package check;
bounded model evaluations; rollback-preserving activation of code and prompt;
live WhatsApp identity/hosting response; gateway readiness and final idle status.

See `docs/assistant-behavior.md` for the behavior contract and evaluation cases.
