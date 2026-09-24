Type: research
Status: resolved
Blocked by: 01

## Question

Given the chosen agent runtime and current model availability, which OpenAI model and reasoning level should be Jarvis's V1 default, which deterministic `/model`, `/reasoning`, and `/config` values are valid, how are working-session overrides separated from persistent defaults, and what cost and fallback policy applies when a requested model is unavailable?

## Answer

Use explicit `gpt-6-sol` with `medium` reasoning as the V1 default on the Agents SDK Responses path. Accept the canonical `gpt-6-astra`, `gpt-6-sol`, `gpt-5.6-terra`, and `gpt-6-luna` model values; aliases `6-astra`, `6-sol`, `5.6-terra`, and `6-luna` resolve to those IDs, and the shorter forms `astra`, `sol`, `terra`, and `luna` remain accepted. All four support `low`, `medium`, `high`, `xhigh`, and `max`; Sol, Terra, and Luna additionally support `none`, while Astra does not. `/model` and `/reasoning` are session-scoped; model-related `/config` changes persistent defaults for future sessions. Validate availability and each model/effort pair; fail closed on unavailable or unsupported choices and never silently downgrade or substitute. See the [default model and cost policy research artifact](../research/default-model-and-cost-policy.md).

## Comments
