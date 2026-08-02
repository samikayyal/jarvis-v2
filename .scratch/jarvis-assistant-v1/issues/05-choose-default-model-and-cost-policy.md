Type: research
Status: resolved
Blocked by: 01

## Question

Given the chosen agent runtime and current model availability, which OpenAI model and reasoning level should be Jarvis's V1 default, which deterministic `/model`, `/reasoning`, and `/config` values are valid, how are working-session overrides separated from persistent defaults, and what cost and fallback policy applies when a requested model is unavailable?

## Answer

Use explicit `gpt-5.6-terra` with `medium` reasoning as the V1 default on the Agents SDK Responses path. Accept only the canonical `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` model values and `none`, `low`, `medium`, `high`, `xhigh`, and `max` reasoning values. `/model` and `/reasoning` are session-scoped; model-related `/config` changes persistent defaults for future sessions. Validate availability and fail closed on unavailable models; never silently downgrade or substitute. See the [default model and cost policy research artifact](../research/default-model-and-cost-policy.md).

## Comments
