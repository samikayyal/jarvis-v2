# Gemini free search grounding

Checked against Google's public documentation on 2026-10-06. No credentials,
project settings, or paid APIs were used or changed. Project-specific access
and quotas remain unverified.

## Published free offering

The Gemini Developer API pricing table lists both `gemini-2.5-flash` and
`gemini-2.5-flash-lite` with free input and output tokens and up to 500 Google
Search grounded requests per day on the free tier. The grounding limit is
shared between Flash and Flash-Lite, not 500 per model.

For the current Gemini 3.x text models inspected, ordinary text generation has
a free tier but Google Search grounding is unavailable in that tier. Their
5,000 included monthly search queries belong to the paid tier, with model
tokens charged separately. This does not meet the operator's strict $0 rule.

Source: [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing).

## Access and capacity are separate constraints

The Gemini 2.5 Flash model page says 2.5 access is limited to users who actively
used those models in the past. It says the models are still served and are not
deprecated. A published free price is therefore not proof that a new project
can use the model.

The pricing-table grounding allowance is not a guarantee of usable model
capacity. Project model requests-per-day, requests-per-minute, and token limits
also apply. Actual project limits must be checked in AI Studio.

Sources:

- [Gemini 2.5 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash)
- [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)

## Proposed zero-cost configuration

Use an eligible Gemini Developer API Free Tier project with billing disabled.
Verify both the project's tier and model access before activation. A key name
or a local request counter is not proof of free-tier billing status. Do not
enable billing, add credits, switch to Vertex, or use paid fallback.

Unavailable access, exhausted quota, and provider errors should follow the
agreed fallback: disclose that web search is unavailable and answer from
existing knowledge without claiming online verification. Details of cooldown
and retry behavior remain an interview decision.

This is a proposed configuration, not an account change. The billing guide
distinguishes Free Tier and Paid Tier project keys and says upgrading requires
linking billing; it also documents disabling project billing to downgrade.

Source: [Gemini billing](https://ai.google.dev/gemini-api/docs/billing).

## Unpaid data handling

The standard Unpaid Services terms allow Google to use submitted content and
generated responses for product/model improvement and allow human review.
They instruct developers not to submit sensitive, confidential, or personal
information. Regional exceptions are described in the terms; they must not be
assumed from a workstation timezone.

This requires refining the previously agreed minimum-context rule: the
recommended free-tier design uses public, non-sensitive questions only. The
operator's acceptance of this narrower scope remains open.

Source: [Gemini terms, Unpaid Services](https://ai.google.dev/gemini-api/terms#unpaid-services).

## Display remains a separate requirement

The Developer API grounding terms require displaying associated Search
Suggestions with the grounded answer. WhatsApp text rendering must be assessed
against the applicable display instructions; the Vertex paid-route exception
does not establish a free Developer API exception.

Source: [Gemini grounding terms](https://ai.google.dev/gemini-api/terms#grounding-with-google-search).
