# Page reader search and coverage

Status: complete

## Requested scope

Implement all three improvements requested by the operator:

1. Search the full captured page text across chunks.
2. Require broader investigation before concluding that requested information was not found.
3. Enforce coverage outcomes in code so incomplete investigation cannot be presented as exhaustive.

The operator requested the grill-with-docs workflow: discuss the design in rounds,
record settled decisions as they arise, and confirm shared understanding before implementation.
Final alignment was confirmed with the operator's instruction to implement Q17
and use 24 model turns, 18 actions, 120 seconds and 128,000 context tokens.
Local runtime implementation and verification are complete. See
[verification.md](verification.md) for checks and their limits. Subsequent deployment
was authorized and completed; visible WhatsApp acceptance is partial, with refresh
interpretation and quote-repair failures recorded in
[deployment-verification.md](deployment-verification.md).

## Consolidated implementation contract

This section incorporates the operator's corrections and takes precedence over
earlier discussion wording below.

1. Add reader-only local text search across captured chunks. Accept 1-20 nonempty
   literal terms/phrases of at most 200 characters each. Match any term using
   case-insensitive matching and normalized whitespace, preserving original source
   text. Return unique matching chunk IDs in capture order, without excerpts,
   ranking or pagination. The reader consults the existing heading index before
   opening chunks. If a complete result cannot fit, report a limit rather than
   returning an incomplete list as complete. Searching does not mark chunks read.
2. Code owns fallback progression on an unresolved answer (Q17 reinstated by the
   operator). Inspect pending matches, require up to two related-term searches
   counting those already performed, require relevant display review, then supply
   remaining unread chunks one at a time in capture order. Search terms and relevant
   display choices remain model judgments. A fully inspected short capture can skip
   unnecessary searching. Stop on supported evidence, captured-content exhaustion
   or a shared limit. No independent reading conversations, embeddings or per-part
   checklist. Repeated refusal to perform a required step consumes the existing
   attempt/action budget and returns an incomplete result, never an absence claim.
3. Keep one retained reader conversation and existing session/refresh/cancellation
   semantics. Reuse previously supplied chunks without inserting duplicate bodies.
   Follow-ups must reassess whether retained evidence answers the new question.
   Successful refresh replaces evidence; a failed refresh preserves old evidence
   but cannot count as fresh verification. Other pages remain Jarvis's responsibility.
4. Code records actual inspection, capture completeness, display state and stopping
   limits, separately from the reader's question-level answered/unresolved judgment.
   Unread content prevents an exhaustive-inspection claim, but does not invalidate
   a supported narrow answer. All captured text inspected does not establish
   semantic absence or inspection of every website state. Relevant display-control
   selection and identification of unresolved facts remain model judgments.
5. An answered result needs at least one validated source quote from inspected
   content. If none survives validation, the reader can repair its evidence within
   the remaining budget; otherwise return unresolved. Quote occurrence is not a
   proof of semantic support for every answer claim.
6. Starting configurable per-invocation limits: 24 model attempts, 18 reader actions,
   120 seconds and 128,000 context tokens. Search, chunk reads and display actions
   share the action allowance. Local search consumes no page visit. Existing page
   budgets, tighter outer deadlines and capture limits still apply. No reset of
   budgets or silent eviction to extend fallback.
7. Preserve source identity, question outcome, consequential coverage limitation
   and stop reason before optional prose when fitting results. Report output_limit
   if essential information cannot fit; never silently drop a limitation while
   returning apparent success.
8. Code ensures unresolved source-specific coverage limitations reach the final
   reply, even if another source answers the question. Wording must identify the
   incomplete source rather than claim the whole answer is unverified. Further
   successful investigation of the same reader question can clear its limitation;
   an unrelated follow-up cannot. No semantic cross-source gap-resolution ledger.
9. Verify tools, accounting and disclosure using repeatable tests and local real
   browser fixtures. Scripted model responses verify mechanics, not real-model
   comprehension. A live public-page check is supplementary if available. Local
   implementation and validation are the next step after final alignment;
   deployment has not been requested.

Design alignment is complete. Earlier rejected/accepted proposals below are the
discussion history; the final Q17 and budget changes above supersede them.

## Design tree

- Result contract: what the system can establish about evidence and absence.
  - Coverage scope per question, captured page, and display state.
  - Partial answers, conflicting evidence, and final-answer enforcement.
- Investigation policy: when broader fallback is mandatory.
  - Text search semantics and result limits.
  - Relevant display controls and alternate states.
  - Exhaustive captured-content review and stopping rules.
- Resource policy: latency, model cost, and bounded progress.
  - Budgets, fallback passes, retention, refresh, and cancellation.
- Validation: adversarial fixtures, regression checks, and acceptance criteria.

## Settled decisions

- All three improvements are in scope; detailed behavior remains under discussion.
- Use the repository's existing CONTEXT.md for domain vocabulary and docs/adr/ for qualifying architectural decisions.

## Accepted first-round decisions (2026-11-06)

- Define the guarantee as evidence discovery plus honest coverage, without claiming proof of absence from an entire website.
- Require broader investigation through reader instructions on unresolved material parts, while allowing early completion when sufficient evidence answers every requested part. Q17 later explicitly places progression in the agent's hands.
- Run fallback automatically within finite configurable budgets, reporting remaining gaps when exhausted.
- Restrict a reader's coverage to its assigned page and relevant display states; the main assistant retains ownership of other pages.

The operator accepted all four recommendations. Coverage records what content was
searched or submitted for review; it cannot establish perfect model comprehension.
An explicit source statement of absence is distinct from failure to find evidence.
An unresolved material part (for example minimum billing duration when price is
already known) requires further investigation. Complete-list questions require
broader coverage than a single supported fact. Relevant display states belong to
the assigned page; separate linked documents remain the main assistant's responsibility.

## Accepted second-round decisions

- Q5 accepted: start with deterministic text search over all captured chunks, with reader-selected related terms; use broader reading to cover wording that text matching misses. No embedding index in the first implementation.
- Q6 corrected and clarified by the operator: search returns matching chunk IDs only, without excerpts. The reader uses the existing index to check a matching chunk's heading before choosing to open it. The reader must open chunks to obtain their content. Search matches do not count as inspected chunks. Headings provide selection context, not proof that a full-text match is irrelevant. Matching rules, ordering, deduplication and pagination remain to be designed.
- Q7 rejected: keep the design simple; do not introduce an explicit checklist or per-question-part status ledger. Preserve the focused question, findings and gaps. The accepted requirement to investigate unresolved material parts remains, but identifying those gaps is model judgment; code will not verify every requested part against a checklist.

## Accepted third-round decisions

- Q8: accept a list of literal terms/phrases, match any term case-insensitively with normalized whitespace, and return unique matching chunk IDs in capture order. Do not introduce fuzzy matching, stemming, regular expressions or ranking. Original source text remains unchanged for evidence.
- Q9: bounded fallback first uses related-term searches and matching chunks, then relevant supported display controls, then remaining captured chunks if unresolved. Stop when evidence is sufficient or a limit is reached; do not dismiss uninspected matches solely on heading wording. Review-pass architecture and budgets remain separate decisions.

The operator accepted Q8 and Q9 together. Exact search size limits and result
pagination remain open; matching and result ordering are settled.

## Accepted fourth-round decisions

- Q10: keep broader reading in the existing retained reader conversation. When context capacity prevents further reading, return incomplete coverage and the specific limit rather than introducing independent review conversations or evicting retained text.
- Q11: reuse retained text and evidence for follow-ups, but reassess whether they answer the new question. Historical inspection counts do not establish that a new question has been answered.
- Q12: use one question-level reader assessment rather than a checklist, and code-owned coverage/limit facts. Block an exhaustive-coverage status when those facts contradict it, and carry consequential incomplete-verification notices to the final-answer boundary. A model's assessment that evidence answers the question remains a semantic judgment.
- Q13: scope display exploration to relevant states, preserve evidence labels, and record known relevant states that could not be checked. Do not enumerate every combination or describe captured-text coverage as coverage of every possible website state.

The operator accepted Q10-Q13 together, requested further explanation of Q12,
and then confirmed the explanation.

### Q12 enforcement boundary

The reader judges whether evidence answers the focused question or leaves a gap.
Application code owns the record of chunk access, incomplete capture, attempted
display work and stopping limits; a model cannot override those facts by claiming
exhaustive coverage. A reported unresolved question calls for the accepted bounded
fallback under agent control (Q17 correction); if further work stops, the result preserves the gap and coverage
limitation. Supported answers do not require reading unrelated remaining content.

This does not guarantee semantic correctness: valid source quotes can be
misinterpreted, and an unrecognized gap can escape the reader's assessment.
Mandatory final disclosures guarantee visibility of recorded unresolved
verification, not the correctness of every sentence in generated prose.
Exact result fields, output-fitting priorities, final-reply integration and how
later evidence resolves an earlier gap remain implementation-design decisions.

## Investigation findings for later rounds

- Current inspected coverage is cumulative across the reader's working-session history, not specific to a question. A chunk being supplied to the model does not establish comprehension or support for a new question.
- ADR 0003 retains selected chunk bodies without silent eviction or summarization. Independent fallback reading passes require an explicit design amendment; large captures cannot always fit in the retained conversation.
- Display operations append captures from different display states. Coverage must distinguish those states rather than pooling them into one completeness claim.
- Current quote validation checks source occurrence, not semantic support for the answer. The final-answer boundary currently has no reader coverage enforcement, and output fitting can drop coverage details.
- Search size limits, pagination, fallback orchestration, display states, budgets, retention and answer enforcement remain open. Q8 settled matching rules and result ordering; Q9 settled fallback ordering.

## Accepted fifth-round decisions

- Q14: accept up to 20 nonempty literal terms/phrases, each at most 200 characters; return all unique matching chunk IDs without excerpts or ranking. No pagination in the initial implementation. If the complete result cannot fit the reader context, return an explicit limit outcome rather than a silently shortened list or a false empty result. Search is local and consumes a reader action but no page/network allowance.
- Q15: starting configurable limits of 16 model attempts and 12 reader actions per invocation, retaining the 120-second reader deadline and 64,000-token context budget. Search, chunk reads and display actions share the action allowance; all fallback shares these limits and any tighter outer request deadline. No budget resets inside fallback.
- Q16: preserve source identity, question outcome, coverage limitation and stopping reason ahead of optional prose when fitting tool results. Shorten the narrative before dropping these facts; a result too small for essential facts must return an explicit output-limit failure, never apparent success with missing coverage.

The operator accepted Q14-Q16 together.

## Accepted sixth-round decisions and correction

- Q17 initially rejected, then explicitly reinstated in the final operator instruction. The final design uses code-driven fallback as described in the consolidated contract.
- Q18: an answered result requires at least one validated supporting quote from inspected content. Invalid quotes are rejected; when none survive, the answer cannot be accepted as supported and must be repaired within the shared budget or returned unresolved. This checks source occurrence, not semantic entailment of every claim.
- Q19: unresolved source-specific coverage failures receive a concise final source-specific notice even when another source supplies an answer. This preserves machine-enforced disclosure without introducing a semantic gap-resolution ledger; it must not claim that the overall question is unverified merely because one page was incomplete. Successful further investigation of the same reader question can clear that notice.
- Q20 accepted: acceptance uses repeatable fixture tests and local real-browser controls tests, covering misleading headings, literal-search misses, display states, follow-ups, incomplete capture, limits, invalid evidence and output fitting. A live public-page check is supplementary if available, not a replacement for repeatable checks. Deployment has not been requested.

The operator accepted Q18-Q20 and corrected Q17 as recorded above. Existing lifecycle behavior
(session-only retention, successful refresh replacement, failed refresh preservation,
cancellation cleanup) is the intended baseline; implementation must preserve it.

## Final correction and implementation authorization

The operator instructed: "actually do 17, make it 24 model turns, 18 actions,
120 seconds and 128k context." This supersedes the earlier agent-directed-only
fallback and 16/12/64k limits. Q17 is implemented with mandatory progression,
retaining the same-model, single-conversation boundary. Local implementation and
tests are authorized; no deployment was requested.
