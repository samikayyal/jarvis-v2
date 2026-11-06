# Ticket 01 implementation verification

Date: 2026-11-06 (Asia/Amman).

Scope: one opt-in asynchronous Playwright reader through the main assistant's
prepared tools. Concurrent scheduling, retained follow-ups, production cutover
and Ubuntu activation remain in later tickets. The default deployed reader path
was not changed and no deployment was attempted.

Review baseline, confirmed by the operator:
`7857b88e2c23bfa1c1d76a832f6b155122d26d0a`.
Initial implementation commit: `15bc40a`.
Verified implementation and review-fix commit:
`2953ec18ba77bf6c1649d870c5315685f429b664`. The final follow-up commit changes
only completion/verification documentation and the saved test output.

## Validation

- Locked uv provisioning and Playwright Chromium installation completed.
- Browser wheel download was checked against PyPI's SHA-256 after normal download
  attempts timed out. Runtime sources retain standard PyPI dependency pins.
- Focused browser/reader, existing web-boundary and main-loop run: 70 passed,
  2 opt-in checks skipped. Skill discovery: 23 passed.
- Separate live screenshot check, with explicit approval to reuse the existing
  API key: `gpt-6-luna`, low reasoning, 2 model turns, 29.09 seconds including
  setup. The reader requested a screenshot and returned the canvas-only code
  `ORCHID 7391` with the fixture URL. No model fallback was used.
- New regression checks first reproduced all three Spec findings; after fixes,
  all 4 selected checks passed, including legitimate public form POST submission.
- Package verification after updating both deployment requirements and the hash
  lock: 6 passed. Only Playwright, greenlet and pyee were added to that lock.
- Repository lint and formatting pass. Focused ty checks pass for the new modules.
  The existing Responses module has 14 ty diagnostics at both the starting commit
  and the candidate; no repository-wide typecheck pass is claimed.
- First full suite: 647 passed, 11 skipped, 1 dependency-manifest failure. The
  package manifest and its expectations were corrected. Final full-suite result:
  **651 passed, 11 skipped in 399.89 seconds**. See the saved
  [final output](verification/full-suite-final.log). Skips cover opt-in and
  platform-dependent fixtures;
  the model screenshot check was separately run as described above.

## Standards

No actionable findings. The reviewed diff follows the documented uv workflow,
issue location/status conventions and the prepared-tool/lifecycle conventions of
the surrounding implementation. The trial path is explicit, keeps the default
reader and performs no deployment. Authorized changes to navigation and findings
on this trial path supersede the older reader contract within ticket 01's scope.
Pre-existing design drafts and `CONTEXT.md` changes were outside the review diff.
The Standards reviewer also checked `15bc40a...2953ec1` after the fixes and found
no actionable issue in dependency synchronization, narrower POST authority,
findings preservation, regression coverage or documentation.

## Spec

The initial review identified three findings:

1. **P1: Untrusted control metadata granted browser-wide POST authority.** Ticket
   01 requires resource creation outside reader authority and says page content
   cannot expand authority. A research-like control enabled unrelated scripted
   POSTs, and an explicit Create resource control could be submitted. Fixed by
   permitting one exact observed research form submission, matching action URL,
   submitting frame, encoded payload and navigation type; resource-creation
   controls are excluded.
2. **P2: Later commentary destroyed explicitly preserved findings.** Ticket 01
   requires explicit limits with available findings. A tool-call response's
   commentary could replace `report_findings` content before reaching a limit.
   Fixed by replacing findings only with a completed written response.
3. **P2: Provider context exhaustion became configuration/image failure.** Ticket
   01 requires explicit context-limit reporting. The provider's
   `context_length_exceeded` now takes the `limit/context_limit` path and retains
   available findings before generic configuration/image-error handling.

The Spec reviewer rechecked the working source delta and confirmed all three
findings resolved, with no remaining concrete concern from that recheck. The
recheck was read-only; the root agent ran the regressions above.

Standards: 0 findings. Spec: 3 initial findings (worst P1), all resolved;
0 remaining findings on either axis.
