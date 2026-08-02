Type: research
Status: resolved
Blocked by:

## Question

What is the exact current Gemini API model identifier and production contract for the intended `3.5-flash-lite` command-risk classifier, including structured-output support, latency and token limits, pricing, rate limits, data-use and retention terms, regional or preview constraints, and behavior when the model is unavailable; and which of those facts constrain its use as an advisory allow-or-request-permission classifier beneath deterministic policy?

## Answer

The exact current stable/GA API identifier is `gemini-3.5-flash-lite`; the model page marks structured outputs as supported and specifies 1,048,576 input / 65,536 output token limits. The generic legacy `generateContent` structured-output model table does not list this exact model, so a future V2 implementation must prefer the currently recommended Interactions API or reverify endpoint-specific support before use. The research originally evaluated it as an advisory classifier beneath deterministic terminal policy. Ticket 07 later removed all model-based command authorization from V1 and deferred this possible use to V2. If reconsidered in V2, a valid `allow` must never override a hard deny or mandatory approval, and any invalid result, timeout, quota/region/lifecycle failure, or uncertainty must fail closed to no auto-execution and exact operator permission. Production should use a paid project, redact secrets, bound retries and latency, avoid state/tools/grounding, and treat Google's dynamic quotas, 55-day abuse-monitoring retention, lifecycle status, and current terms as operational constraints. Google's Additional Terms also limit Gemini API use to developers building for professional or business purposes rather than consumer use, require API users to be 18 or older, restrict access to available regions, and require paid services when an API client is made available to users in the EEA, Switzerland, or the UK; Jarvis's V2 eligibility under those terms remains unresolved. See the [Gemini classifier contract research artifact](../research/gemini-classifier-contract.md).

## Comments
