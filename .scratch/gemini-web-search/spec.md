# Gemini web search

Status: Superseded by Selenium web search on 2026-10-06

The operator replaced Gemini with Selenium-controlled Google searches during
the design interview. Continue in
[Selenium web search](../selenium-web-search/spec.md). This file and its research
remain as history; the direct-Gemini-answer decision below is no longer active.

## Purpose

Add Gemini-powered web assistance to Jarvis while minimizing additional cost.
This document records an ongoing grill-with-docs interview. It is not an
implementation approval or a completed specification.

## Agreed behavior

Confirmed by the operator on 2026-10-06:

- Support quick lookups and moderate research. The model chooses the appropriate
  depth for the request.
- Search automatically when freshness or verification matters, and when the
  operator explicitly requests a search. Stable questions normally use the
  existing model without searching.
- Additional Gemini/search cost must be strictly $0, including model tokens.
  Free daily allowances on Gemini Flash models must be researched and actual
  project eligibility verified. Paid tokens, paid overages, and paid provider
  fallback are not authorized.
- If web search is unavailable, tell the operator that it is unavailable and
  answer from the model's existing knowledge. Do not imply that the answer was
  verified online.
- OpenAI decides when and what to search; Gemini produces the successful direct
  web answer. Jarvis displays that answer with its citations without asking
  OpenAI to rewrite it. Ordinary conversation and unavailable-search fallback
  remain with OpenAI.
- Send Google only a self-contained question and minimum relevant conversation
  details. Private vault, email, or calendar content requires the operator's
  explicit request; do not send the entire working session by default.

## Decisions still open

- Which Developer API Flash model offers free grounding and is available to the
  actual project; how to enforce zero additional charges.
- How required Search Suggestions can be displayed in Jarvis's WhatsApp
  interface on a zero-cost API route.
- Whether the unpaid service's data-use conditions fit the intended inputs.
- Tool exposure and skill integration; exact inputs and outputs; search-depth,
  time, cancellation, retry, and response-length limits.
- Follow-up requests, retained conversation context, and runtime traces.
- Behavior when an answer requires current facts that the fallback model cannot
  establish, and when Gemini answers without actually performing a search.
- Configuration, credentials, validation, packaging, and eventual activation.

## Current evidence

The local checkout has an OpenAI Responses model-and-tool loop, a WhatsApp text
interface, and unrelated in-progress runtime-skill and vault changes. Those
changes must be preserved. Current source and provider documentation are being
reviewed before proposing an integration.

Google Search grounding produces a synthesized answer and citation metadata.
Its published terms restrict transforming or reusing grounded results and
extracting their links for crawling. This means a generic search-result tool
feeding OpenAI must not be assumed to be the permitted integration.

Google Cloud's current Gemini grounding pricing allows Search Suggestions to be
omitted below one million grounded prompts per day at standard pricing, unlike
the standard Developer API display requirement. This may make a direct-answer
integration suitable for WhatsApp, but does not remove the restrictions on
transforming or reusing grounded results. It is not a zero-cost solution:
included grounding queries on the paid tier do not eliminate model-token fees.
The operator subsequently ruled out paid usage.

References checked during the interview:

- [Free-tier research and remaining prerequisites](free-tier-research.md)
- [Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search)
- [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini Developer API terms](https://ai.google.dev/gemini-api/terms#grounding-with-google-search)
- [Google Cloud grounding pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)
- [Google Cloud service terms](https://cloud.google.com/terms/service-terms)

## Interview history

### Provider change — 2026-10-06

The operator selected Selenium instead of Gemini. Carry forward the settled
scope, automatic trigger, zero additional search-service spending, minimum
query context, and unavailable-search fallback. Gemini model selection,
grounding terms, and direct answer presentation are superseded. The proposed
timeouts and answer lengths had not been accepted before this change.

### Round 1 — 2026-10-06

1. Scope: quick lookups and moderate research; model decides the depth.
2. Trigger: automatic when useful for freshness/verification, plus explicit
   operator requests.
3. Cost preference: free if possible; exact spending policy not yet decided.
4. Failure: disclose unavailable web search and answer from existing knowledge.

### Round 2 — 2026-10-06

5. Answer ownership: accepted direct Gemini answer; OpenAI selects the search
   request but does not rewrite the successful answer.
6. Cost: strictly $0 additional charges; research free daily allowances on
   Gemini Flash models before selecting a model or credential route.
7. Context: accepted minimum question/conversation context, with private tool
   content only on an explicit operator request.
