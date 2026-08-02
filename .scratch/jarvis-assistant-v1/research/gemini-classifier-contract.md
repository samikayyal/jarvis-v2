# Gemini classifier contract

Research date: 2026-08-01

> **V2-only background:** Ticket 07 subsequently removed Gemini and every other
> model-based classifier from V1 terminal authorization. The research below is
> retained for a possible V2 design and is not part of the V1 architecture.

Scope: Verify the current Gemini API contract for Jarvis's intended `3.5-flash-lite` command-risk classifier. This note evaluates Gemini only as an advisory allow-or-request-permission signal beneath deterministic terminal policy. It does not authorize Gemini to execute commands, override a hard prohibition, or replace exact operator approval.

No paid model API call was made. The findings below come from current first-party Google documentation only.

## Decision

Use the stable Gemini API model ID `gemini-3.5-flash-lite` if Jarvis keeps a Gemini advisory classifier. The model is a current GA/stable model, supports structured outputs, and is positioned by Google for low-latency, high-throughput, low-cost work. It is suitable as a bounded second opinion for actions that deterministic policy has already classified as eligible for possible auto-approval.

The classifier must remain advisory:

1. Deterministic policy evaluates the canonical command, arguments, working directory, target host, session permissions, and mandatory-approval or hard-deny rules first.
2. Gemini may be called only for a deterministic-policy candidate that is eligible for advisory classification. Gemini cannot make a prohibited action eligible.
3. A valid model result of `allow` means only “the advisory classifier did not identify a reason to require permission.” It is not final authorization.
4. Any timeout, transport/API error, quota or regional failure, model lifecycle failure, blocked response, malformed or semantically invalid JSON, unexpected field, or uncertain result becomes `request_permission`/no auto-execution. Deterministic hard denies still deny immediately.
5. Exact operator approval remains required for approval-gated actions, and approval is for the frozen exact proposal only.

## Confirmed current facts

### Identity, lifecycle, and capabilities

- The exact current model code is `gemini-3.5-flash-lite`. The model page marks the stable version as `gemini-3.5-flash-lite`; the model is not identified as a preview or as a `-001` version. The page lists structured outputs as supported. [Gemini 3.5 Flash-Lite model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- Google lists Gemini 3.5 Flash-Lite as generally available and stable, released July 21, 2026, with no shutdown date announced as of the research date. “No shutdown date announced” is not a guarantee of indefinite availability. [Gemini deprecations](https://ai.google.dev/gemini-api/docs/deprecations) · [Release notes](https://ai.google.dev/gemini-api/docs/changelog)
- Google describes the model as its fastest, lowest-cost 3.5 model for high-throughput execution and specifically recommends the default `thinking_level` of `minimal` for high-volume extraction, routing, or classification. [Using the latest Gemini models](https://ai.google.dev/gemini-api/docs/latest-model)
- The model accepts text, image, video, audio, and PDF input and returns text. The classifier should send text only: the canonical command representation and the minimum policy context needed for advisory classification. [Gemini 3.5 Flash-Lite model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)

### Token and latency contract

- The model page specifies an input limit of 1,048,576 tokens and an output limit of 65,536 tokens. The latest-model guide rounds the output figure to 64k. A Jarvis classifier should set a much smaller application-level output cap because it needs only a small enum object, and should bound the input to the canonical command and relevant policy facts. [Gemini 3.5 Flash-Lite model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite) · [Using the latest Gemini models](https://ai.google.dev/gemini-api/docs/latest-model)
- Google publishes qualitative low-latency/high-throughput positioning, but the reviewed model and pricing documentation does not provide a Jarvis-grade p95/p99 latency target or an end-to-end latency SLA. Therefore, the application must choose and measure its own timeout, retry budget, and user-visible fallback. This is bounded uncertainty, not evidence that the model will meet a particular response time.

### Structured output

- Google documents structured outputs for classification. The API accepts a JSON Schema subset and supports object, string, number, integer, boolean, array, and null types; object properties can be required and strings can use enums. [Generate Content structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output) · [Structured outputs overview](https://ai.google.dev/gemini-api/docs/structured-output)
- The exact model page lists structured outputs as supported. Structured output constrains the response shape; it does not prove that the model's risk judgment is correct or that the command is safe. The latter is the reason deterministic policy remains authoritative. [Gemini 3.5 Flash-Lite model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- The exact model page and the Interactions API's supported-model list include `gemini-3.5-flash-lite`, but the legacy `generateContent` structured-output page's model table does not list this exact model. Treat that as endpoint/documentation uncertainty and reverify legacy endpoint support before any V2 implementation; generic structured-output support alone is not sufficient evidence for that choice. [Gemini 3.5 Flash-Lite model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite) · [Interactions API overview](https://ai.google.dev/gemini-api/docs/interactions-overview) · [Generate Content structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
- Google now recommends the Interactions API for new development, while stating that the original `generateContent` API remains fully supported. The legacy API documents structured outputs and is stateless, but the exact model-specific support needs re-verification because its structured-output model table omits `gemini-3.5-flash-lite`. If Interactions is used, set `store=false` when the intended contract is no server-side interaction storage; Google documents that this control is separate from state management and prevents `previous_interaction_id` on subsequent turns. [Interactions API overview](https://ai.google.dev/gemini-api/docs/interactions-overview) · [Migrating to the Interactions API](https://ai.google.dev/gemini-api/docs/migrate-to-interactions) · [Generate Content structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
- Gemini 3.5 Flash-Lite and future Gemini model generations deprecate `temperature`, `top_p`, and `top_k`; Google says to remove them. Configure the documented thinking level rather than relying on those sampling parameters. [Using the latest Gemini models](https://ai.google.dev/gemini-api/docs/latest-model)

### Pricing

For the interactive Standard paid tier, Google currently lists:

| Item | Current price |
| --- | ---: |
| Input, per 1M tokens | $0.30 |
| Output, including thinking tokens, per 1M tokens | $2.50 |
| Context caching, per 1M tokens | $0.03, plus $1.00 per 1M tokens/hour storage |

The same page lists free-tier input/output as free of charge, but the free tier has different data-use terms and limited/dynamic quotas. Batch and Flex are listed at $0.15 input and $1.25 output per 1M tokens; they are not a default fit for an interactive approval decision. Priority is listed at $0.54 input and $4.50 output per 1M tokens. Output pricing includes thinking tokens, so `thinking_level=minimal` and a small output cap are cost controls. [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)

### Rate limits and service errors

- Gemini API limits are evaluated across requests per minute (RPM), input tokens per minute (TPM), and requests per day (RPD); limits are per project, not per API key. RPD resets at midnight Pacific time. Limits vary by model and usage tier, and actual capacity may vary even when a displayed limit exists. [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- Current limits are account/project-specific and should be read from Google AI Studio. The documentation does not provide one immutable RPM/TPM/RPD table that can be used as Jarvis's production guarantee. The page also documents spend-based rolling ten-minute limits of $10 for Tier 1 and $200 for Tiers 2 and 3; a spend-limit breach returns `429 RESOURCE_EXHAUSTED`. [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- Google recommends exponential backoff with jitter for retryable `429`, `408`, and `5xx` errors and says not to retry client errors such as `400` or `403`. The Python SDK documentation example says its default transient retry behavior can retry up to four times, with roughly one-second initial delay and up to 60 seconds maximum delay. Jarvis should use an explicit smaller bounded retry budget for an approval-path classifier rather than allowing a generic SDK retry policy to hold the request open. [Troubleshooting guide](https://ai.google.dev/gemini-api/docs/troubleshooting)
- A classifier response that cannot be obtained within Jarvis's bounded budget is not an `allow`; it is an unavailable/unknown result handled by the deterministic fallback below.

### Data use, logging, and retention

- Under Google's current terms, unpaid Gemini API quota and direct free services may use submitted content and generated responses to provide, improve, and develop Google products and machine-learning technologies; human reviewers may process that data. Google says not to submit sensitive, confidential, or personal information to unpaid services. [Gemini API Additional Terms](https://ai.google.dev/gemini-api/terms)
- Paid services do not use prompts or responses to improve Google's products, but paid-service prompts and responses can still be logged for a limited period for abuse monitoring and required legal or regulatory disclosures. Google's abuse-monitoring documentation says prompts, contextual information, and outputs are retained for 55 days and may be assessed by authorized Google employees when flagged. [Gemini API Additional Terms](https://ai.google.dev/gemini-api/terms) · [Abuse monitoring](https://ai.google.dev/gemini-api/docs/usage-policies)
- For billing-enabled projects, Google AI Studio's developer-owned API logs are separate from abuse-monitoring logs. The logs page says the default maximum project-log retention is 55 days, with configurable 7-, 14-, 28-, or 55-day deletion marking. Datasets can retain selected logs beyond that window and can be shared with Google under unpaid-service terms. Do not enable dataset sharing for classifier requests, and do not put secrets or confidential values into the classifier input. [Data Logging and Sharing](https://ai.google.dev/gemini-api/docs/logs-policy)
- The current Interactions overview says stored interactions are retained for 55 days on the paid tier and 1 day on the free tier; `store=false` opts out of default interaction storage. The Gemini zero-data-retention guidance says Paid Services can use a zero-data-retention arrangement only with the documented conditions, including approval for sanitized abuse-monitoring logging. `store=false` does not eliminate abuse-monitoring processing. Do not use grounding, File API uploads, or explicit context caching for this classifier. [Interactions API overview](https://ai.google.dev/gemini-api/docs/interactions-overview) · [Zero data retention in the Gemini Developer API](https://ai.google.dev/gemini-api/docs/zdr)

### Regional and availability constraints

- Google AI Studio and the Gemini API are country/territory restricted. Google's current list includes Jordan, but production provisioning must still verify the actual project, account, and egress conditions. The page also states that access can depend on age/account verification and directs unsupported regions to Gemini Enterprise Agent Platform. [Available regions for Google AI Studio and Gemini API](https://ai.google.dev/gemini-api/docs/available-regions)
- Google's Additional Terms say the Gemini API and AI Studio are for developers building with Google AI models for professional or business purposes, not consumer use; require the API user to be at least 18; require access from an available region; and require Paid Services when an API client is made available to users in the EEA, Switzerland, or the UK. Jarvis's fit under those terms is an unresolved V2 eligibility constraint, not a V1 decision. [Gemini API Additional Terms](https://ai.google.dev/gemini-api/terms)
- Region access is not equivalent to free-tier eligibility. Google's available-regions page confirms country/territory restrictions, while project billing, tier, and active limits remain deployment-time conditions. The exact regional free-tier error wording is a dynamic troubleshooting detail, not a stable API contract; verify the actual project in AI Studio. A production privacy and availability baseline should therefore use a billing-enabled paid project, subject to the applicable terms and budget cap. [Available regions for Google AI Studio and Gemini API](https://ai.google.dev/gemini-api/docs/available-regions) · [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) · [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- The reviewed country-availability documentation is not a data-residency promise and does not establish a Jarvis-specific latency guarantee. Do not infer either from country availability.

## Recommended Jarvis contract

### Request boundary

For each candidate terminal action, Jarvis should construct a canonical, deterministic representation before the model call containing only:

- the exact command and argument vector;
- the exact working directory;
- the selected execution host;
- the deterministic policy result and the narrow policy facts relevant to the decision; and
- a prompt instruction that the model is advisory and cannot authorize execution.

Secret-bearing arguments, access tokens, cookies, private message content, and unrelated vault or connected-service content must be removed or redacted before the request. The classifier should be a single-turn, text-only call with no Gemini tools, grounding, code execution, URL context, file upload, or conversation history.

### Output contract

Use a closed schema equivalent to:

```json
{
  "type": "object",
  "properties": {
    "advisory": {
      "type": "string",
      "enum": ["allow", "request_permission"]
    },
    "reason_code": {
      "type": "string",
      "enum": ["candidate_safe", "sensitive_or_ambiguous", "uncertain"]
    }
  },
  "required": ["advisory", "reason_code"],
  "additionalProperties": false
}
```

The application must validate the response against the schema and against cross-field rules. For example, `reason_code=uncertain` must not pair with `advisory=allow`. Free-form rationale is unnecessary for the authorization path and increases data exposure and parsing surface; if an operator-facing explanation is desired, generate it separately and never treat it as policy evidence.

Recommended request settings are the exact stable model ID `gemini-3.5-flash-lite`, `thinking_level=minimal`, a small application-level output limit, no deprecated sampling parameters, and no server-side interaction storage. If using the current recommended Interactions API, explicitly set `store=false`; if using `generateContent`, keep the request single-turn and do not introduce server-side conversation state.

### Deterministic result handling

| Deterministic policy state | Gemini result | Effective action |
| --- | --- | --- |
| Hard prohibition | Not called | Deny |
| Mandatory approval | Not called or ignored | Request the exact operator approval |
| Advisory candidate | Valid `allow` with valid cross-field checks | May auto-execute only if every deterministic policy check still passes |
| Advisory candidate | Valid `request_permission` | Freeze the exact proposal and request approval |
| Advisory candidate | Timeout, 408/429/5xx after bounded retry, 400/403, region/lifecycle failure, blocked output, malformed JSON, unexpected field, or any uncertainty | No auto-execution; request exact permission or deny according to deterministic policy |

The fallback must not silently substitute another Gemini model. A replacement would need a separately verified model identifier, capability/structured-output contract, lifecycle status, data terms, cost, and evaluation result. Until then, unavailable Gemini means conservative human review.

## Bounded uncertainty and verification still required

- Google does not publish a classifier-specific accuracy guarantee, command-risk benchmark, or p95/p99 latency SLA in the reviewed sources. Jarvis must establish its own offline corpus and production telemetry before enabling auto-approval, and should be able to disable advisory auto-approval without disabling safe manual approval flow.
- Rate limits and actual service capacity are dynamic. A successful documentation review does not prove that a particular Google project is provisioned, billed, region-eligible, or currently serving the model.
- Paid-service terms reduce product-improvement use but do not mean that prompts never leave the Google service or are never retained: abuse monitoring, project logging, optional datasets, legal disclosures, and Interactions storage have separate controls.
- Model lifecycle status must be rechecked before implementation and periodically thereafter. The current page's “no shutdown date announced” should be treated as a snapshot, not a permanent dependency guarantee.

## Sources

All sources below are Google-owned documentation and were checked on 2026-08-01:

- [Gemini 3.5 Flash-Lite model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- [Using the latest Gemini models](https://ai.google.dev/gemini-api/docs/latest-model)
- [Generate Content structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
- [Structured outputs overview](https://ai.google.dev/gemini-api/docs/structured-output)
- [Interactions API overview](https://ai.google.dev/gemini-api/docs/interactions-overview)
- [Migrating to the Interactions API](https://ai.google.dev/gemini-api/docs/migrate-to-interactions)
- [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Troubleshooting guide](https://ai.google.dev/gemini-api/docs/troubleshooting)
- [Gemini deprecations](https://ai.google.dev/gemini-api/docs/deprecations)
- [Release notes](https://ai.google.dev/gemini-api/docs/changelog)
- [Available regions for Google AI Studio and Gemini API](https://ai.google.dev/gemini-api/docs/available-regions)
- [Gemini API Additional Terms](https://ai.google.dev/gemini-api/terms)
- [Abuse monitoring](https://ai.google.dev/gemini-api/docs/usage-policies)
- [Data Logging and Sharing](https://ai.google.dev/gemini-api/docs/logs-policy)
- [Zero data retention in the Gemini Developer API](https://ai.google.dev/gemini-api/docs/zdr)
