# Default model and cost policy

Research date: 2026-08-01

## Decision

Jarvis Assistant V1 should explicitly run the OpenAI Responses path through the
Agents SDK with:

```text
default model:     gpt-6-sol
default reasoning: medium
execution mode:    standard (do not enable pro by default)
verbosity:         medium
```

Sol is the best V1 default for Jarvis's mixed workload because GPT-6 Sol is
built for complex coding and agentic workflows. Jarvis is a reactive,
single-operator assistant with multi-step tool use and approval boundaries, so
the workhorse model is a better starting policy than either the flagship
capability role of Astra or the cost-sensitive/high-volume role of Luna.
This model choice and the `medium` reasoning level are a Jarvis
recommendation derived from the product contract, not an OpenAI-mandated
default. OpenAI's guidance calls `medium` a balanced starting effort, while
reserving `max` for the hardest quality-first work.
[OpenAI model selection](https://developers.openai.com/api/docs/models) ·
[OpenAI GPT-6 model guidance](https://developers.openai.com/api/docs/guides/latest-model)

The model ID and reasoning effort must be passed explicitly through the
Agents SDK `Agent`/`RunConfig`/`ModelSettings` path. Do not inherit the SDK's
implicit default, which is currently `gpt-5.4-mini` with
`reasoning.effort="none"` and `verbosity="low"`. The SDK documents both
run-level model overrides and the `OPENAI_DEFAULT_MODEL` environment variable;
Jarvis should keep the authoritative default in its own persistent
configuration and pass it to each run so that the working-session contract is
visible and testable.
[Agents SDK model selection](https://openai.github.io/openai-agents-python/models/) ·
[Agents SDK run configuration](https://openai.github.io/openai-agents-python/running_agents/)

## Confirmed current OpenAI facts

The following are facts from current first-party OpenAI sources, observed on
the research date:

- The selected variants are `gpt-6-astra`, `gpt-6-sol`, `gpt-5.6-terra`, and
  `gpt-6-luna`. OpenAI describes Astra as its most capable model for hard
  end-to-end work, Sol for complex coding and agentic workflows, Terra as
  balancing intelligence and cost, and Luna for focused, high-volume tasks.
  Jarvis uses explicit variant IDs rather than an unqualified model alias.
- All four variants support `low`, `medium`, `high`, `xhigh`, and `max`
  reasoning effort. Sol, Terra, and Luna additionally support `none`; Astra
  does not. `minimal` is not accepted by this model set.
- OpenAI recommends the Responses API for reasoning, tool-calling, and
  multi-turn workflows. The Agents SDK recommends its OpenAI Responses model
  path for OpenAI-only applications.
- The Agents SDK's current implicit model default is `gpt-5.4-mini`, with
  `reasoning.effort="none"` and `verbosity="low"`. A run-level `RunConfig`
  model overrides the model on each agent for that run, and `model_settings`
  provides global model settings for that run.
- Pricing is a release-time concern rather than an application constant.
  Recheck the current OpenAI pricing page when activating a model policy; the
  deployed configuration must not infer pricing from a model name.
- OpenAI's pricing guidance says a monthly budget can stop serving requests,
  but enforcement may be delayed and overage remains the customer's
  responsibility; it also supports monthly email notification thresholds.
- The Models API exposes list and retrieve operations. The model object has an
  `id` and ownership metadata. Jarvis should use the project's authenticated
  model-list/retrieve response as an availability preflight; documentation
  availability alone is not proof that the Jarvis project's API key can use a
  model.

Sources: [OpenAI models](https://developers.openai.com/api/docs/models),
[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra),
[GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol),
[GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra),
[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model),
[OpenAI API pricing](https://openai.com/api/pricing/),
[OpenAI Agents SDK models](https://openai.github.io/openai-agents-python/models/),
[OpenAI Agents SDK run configuration](https://openai.github.io/openai-agents-python/running_agents/),
[OpenAI Models API reference](https://developers.openai.com/api/reference/resources/models)

## Deterministic V1 command contract

Commands are parsed by Jarvis before model execution. The model cannot emit a
command to change its own model or effort, and natural-language requests do
not count as configuration commands.

### Session-scoped commands

These commands change only the current Jarvis working session and affect the
next request in that session. They do not alter the persistent default.

| Command | Valid values and effect |
| --- | --- |
| `/model` | Show the effective session model, whether it is an explicit session override or the persistent default, and the availability status. |
| `/model gpt-6-astra`, `/model 6-astra`, or `/model astra` | Use Astra for subsequent requests in this working session. |
| `/model gpt-6-sol`, `/model 6-sol`, or `/model sol` | Use Sol for subsequent requests in this working session. |
| `/model gpt-5.6-terra`, `/model 5.6-terra`, or `/model terra` | Use Terra for subsequent requests in this working session. |
| `/model gpt-6-luna`, `/model 6-luna`, or `/model luna` | Use Luna for subsequent requests in this working session. |
| `/model default` | Clear the session model override; subsequent requests inherit the persistent default. |
| `/reasoning` | Show the effective session effort and whether it is a session override or the persistent default. |
| `/reasoning none` | Set session reasoning effort to `none` for Sol, Terra, or Luna; Astra rejects it. |
| `/reasoning low` | Set session reasoning effort to `low`. |
| `/reasoning medium` | Set session reasoning effort to `medium`. |
| `/reasoning high` | Set session reasoning effort to `high`. |
| `/reasoning xhigh` | Set session reasoning effort to `xhigh`. |
| `/reasoning max` | Set session reasoning effort to `max`; this is valid but not the default because it is reserved for explicitly chosen quality-first work. |
| `/reasoning default` | Clear the session reasoning override; subsequent requests inherit the persistent default. |

Only the four canonical model IDs above are sent to the model API. The local
configuration boundary accepts the aliases `6-astra`, `6-sol`, `5.6-terra`,
and `6-luna` plus the shorter forms `astra`, `sol`, `terra`, and `luna`,
resolving each to its same-named model variant. An unknown ID,
unsupported model/effort pair, or effort outside `none`, `low`, `medium`,
`high`, `xhigh`, and `max` is a local validation error; it must not reach the
model API.

### Persistent-default commands

The following model-related `/config` forms change the persistent Jarvis
default for future working sessions. They require the authorized operator and
are recorded as configuration changes. They do not rewrite an existing
working session or an active request.

| Command | Effect |
| --- | --- |
| `/config` or `/config show` | Show persistent defaults, the canonical allowlists, the effective current session values, and the last availability check. |
| `/config default-model gpt-6-astra` | Set the persistent default model to Astra after availability validation. |
| `/config default-model gpt-6-sol` | Set the persistent default model to Sol after availability validation. |
| `/config default-model gpt-5.6-terra` | Set the persistent default model to Terra after availability validation. |
| `/config default-model gpt-6-luna` | Set the persistent default model to Luna after availability validation. |
| `/config default-reasoning none\|low\|medium\|high\|xhigh\|max` | Set the persistent default reasoning effort. |
| `/config reset-defaults` | Restore the V1 defaults: `gpt-6-sol` and `medium`. |

The exact parser should treat the vertical bar above as notation for the
allowed alternatives, not as a character the operator types. In particular,
there is no free-form `/config` key that can inject a model ID, provider,
reasoning mode, or cost setting.

The existing V1 `/config` mechanism may later gain other deterministic fields,
such as the working-session inactivity boundary. That does not change this
ticket's model-policy grammar. `/new` ends the current working session and the
next session inherits the current persistent defaults.

## Session versus persistent state

Jarvis should store these fields separately:

```text
persistent_model_default:     gpt-6-sol
persistent_reasoning_default: medium
session_model_override:       null or one canonical model ID
session_reasoning_override:   null or one allowed effort
effective_model:              resolved per request
effective_reasoning:          resolved per request
```

Resolution is deterministic: a non-null session override wins; otherwise the
persistent default is used. The effective pair is captured in the request
audit record before the first model turn. `/model` and `/reasoning` update only
the current session record. `/config default-*` updates only persistent
defaults and must not alter a running request, an existing pending action, or
an already-open session. The next session created after `/new` reads the
persistent values.

This preserves the existing V1 boundary that model and reasoning choices are
temporary working-session state, while allowing the operator to change the
default deliberately. It also prevents an agent turn from changing its own
future model or reasoning level.

V1 should use standard Responses execution and should not expose
`reasoning.mode=pro` or `reasoning.context` as chat commands. Pro mode is an
explicit API setting that OpenAI says increases model work, latency, and token
usage; it is not an appropriate hidden cost change. If implementation enables
persisted reasoning context, it must be an explicit, tested runtime setting
whose history behavior matches Jarvis's temporary working-session contract;
it is not part of this command surface.
[GPT-6 model guidance](https://developers.openai.com/api/docs/guides/latest-model) ·
[Agents SDK Responses model path](https://openai.github.io/openai-agents-python/models/)

## Cost policy

1. Use standard processing for reactive WhatsApp requests. Do not route an
   interactive request through Batch or Flex merely to reduce cost; those modes
   change latency/availability characteristics that the V1 interaction has not
   selected.
2. Use Sol/`medium` as the baseline. Allow Astra, Terra, and Luna as explicit
   operator choices through `/model`. Allow only the selected model's
   documented effort values through `/reasoning`, keep `medium` as the default,
   and require the operator to choose `high`, `xhigh`, or `max` explicitly.
3. Do not promise a fixed dollar amount per message. The actual bill depends on
   input, cached-input, output, reasoning, tool, and possibly context-length
   usage. Record model, reasoning setting, token usage, tool calls, and request
   outcome for each request; compare the aggregate with the OpenAI usage
   dashboard.
4. Configure an OpenAI project monthly budget and an email alert threshold.
   Treat the platform budget as a backstop, not a precise hard cap, because
   OpenAI documents delayed enforcement and possible overage. Add an
   application-side usage guard once the implementation chooses an operator
   budget value; that value is not specified here because no user budget was
   provided.
5. Do not silently switch to a cheaper model, lower reasoning effort, the
   Agents SDK implicit default, or a different provider to stay under budget.
   Any cost-saving change must be an explicit `/model`, `/reasoning`, or
   persistent `/config` change and must be visible in `/status`/`/config show`.

## Unavailable-model policy

OpenAI's model catalog and API reference establish IDs and availability
operations, but they do not define Jarvis's user-facing fallback policy. The
following is the V1 recommendation:

- Keep a static, reviewed allowlist containing only the four canonical model
  IDs above. At deployment/startup, validate the configured persistent default
  against the project's model availability/permission response. Revalidate an
  explicit `/model` choice before committing it to the session.
- If an operator supplies an unknown model ID, reject it locally and leave the
  current session or persistent default unchanged.
- If a canonical model is documented but unavailable to the current project,
  reject the change, report that the model is unavailable, list the allowed
  alternatives whose availability is known, and leave the prior setting
  unchanged. Do not silently fall back.
- If the persistent default is unavailable when a new session starts, mark the
  assistant model path unavailable and return a deterministic operator-facing
  error that asks for an explicit available `/model` choice. Do not silently
  replace it with `gpt-6-astra`, `gpt-6-sol`, `gpt-5.6-terra`, `gpt-6-luna`,
  or any other fallback.
- If a request fails after a valid model was selected, classify the provider
  error. A transient transport/rate-limit failure may receive a bounded retry
  using the same model and settings. An invalid-model, permission, or
  unsupported-feature failure must fail the request without model substitution.
  The exact retry count and provider error mapping are implementation tests,
  not a reason to change the model silently.
- A failed `/model` or `/config default-model` command is atomic: no partial
  state change, no new request, and no change to a pending approval-gated
  action.

This fail-closed behavior preserves deterministic capability and cost. The
operator can still change model or reasoning at any time through the explicit
commands, but every change is allowlisted, availability-checked, and visible.

## Bounded uncertainty and implementation checks

- This artifact records current public documentation, not the entitlement or
  rate-limit state of the eventual OpenAI project. The resolver required by the
  OpenAI-docs workflow was attempted twice on Windows, but Node failed before
  starting it with `EPERM` while resolving `C:\Users\kayya`; therefore no
  resolver JSON or exact returned guide URLs were available. The official
  `latest-model` guide was fetched directly through the permitted official
  domain fallback instead. Re-run the resolver in an environment that can
  read the skill path before treating its dynamic URLs as confirmed.
- Verify the three model IDs and the chosen effort against the deployed API
  project's model permissions during implementation. Do not make a paid model
  call as part of configuration validation.
- Recheck prices and model support at implementation/release time. The prices
  in this note are a dated snapshot and must not become an unreviewed permanent
  pricing table.
- Test command parsing, session/persistent precedence, `/new`, atomic rejected
  changes, unavailable defaults, and no-silent-fallback behavior with a fake
  provider before any live smoke test.

## Sources

- [OpenAI API models](https://developers.openai.com/api/docs/models)
- [OpenAI GPT-6 model guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [OpenAI API pricing](https://openai.com/api/pricing/)
- [OpenAI Agents SDK models](https://openai.github.io/openai-agents-python/models/)
- [OpenAI Agents SDK running agents and RunConfig](https://openai.github.io/openai-agents-python/running_agents/)
- [OpenAI Models API reference](https://developers.openai.com/api/reference/resources/models)
