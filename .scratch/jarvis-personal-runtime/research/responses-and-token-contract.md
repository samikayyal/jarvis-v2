# Confirm the direct Responses and token-counting contract

Research date: 2026-08-29

## Decision

The simplified Jarvis runtime can use the official Python Responses API
directly. Jarvis should own an in-memory `input` list and make one Responses
request per model turn with `store=False` and `parallel_tool_calls=False`.
When a response contains a function call, Jarvis appends the complete response
output to that list, executes the one requested prepared tool, appends a
`function_call_output` item with the matching `call_id`, and makes the next
Responses request. It repeats until the response has final text or a local
tool-round limit is reached.

The user-selected context gate is implementable as a deterministic local
budget using `tiktoken.get_encoding("o200k_base")`. The gate must count a
canonical representation containing the system instructions, all configured
tool schemas, the complete in-memory input history, and the candidate inbound
message. A projected count at or above the configured limit (100,000 by
default) ends the session before making the request. This is deterministic for
Jarvis, but it is not an exact prediction of the server's input-token count;
the official token-counting documentation says that tools, structure, and
model-specific behavior add tokens that local `tiktoken` counting does not
fully model.

## 1. Python request shape

The current Python SDK exposes `client.responses.create(...)` and accepts the
Responses fields needed by Jarvis: `model`, `instructions`, `input`, `tools`,
`reasoning`, `parallel_tool_calls`, `store`, `truncation`, and `timeout`.
The generated Responses resource sends those values as the JSON body of
`POST /responses`; nested request values are typed dictionaries, while parsed
Responses are Pydantic models.

A normal non-streaming turn should have the following logical shape (the
actual model and effort come from Jarvis TOML configuration):

```python
request = {
    "model": configured_model,
    "instructions": system_prompt,
    "input": input_items,
    "tools": prepared_tools,
    "parallel_tool_calls": False,
    "store": False,
    # Make the no-silent-truncation policy explicit.
    "truncation": "disabled",
}

if configured_reasoning_effort is not None:
    request["reasoning"] = {"effort": configured_reasoning_effort}

response = await client.responses.create(**request)
```

`truncation="disabled"` is the Responses default, and the API reference says
that an input which would exceed the model context window then fails rather
than dropping items. This matches the Jarvis decision to end the session when
its own token gate is reached. Do not opt into `truncation="auto"`, because
that would silently discard the beginning of the conversation.

Responses function tools use the direct Responses shape, not the older Chat
Completions `function` wrapper:

```python
{
    "type": "function",
    "name": "read_vault",
    "description": "Search or read bounded Markdown from the configured vault.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
    "strict": True,
}
```

The function-calling guide identifies `type`, `name`, `description`,
`parameters`, and `strict` as the function definition fields. It also
recommends strict schemas; the implementation should still validate every
argument in the local handler before doing anything with it.

Sources: [Responses create reference](https://developers.openai.com/api/reference/resources/responses/methods/create),
[function definition fields](https://developers.openai.com/api/docs/guides/function-calling#defining-functions),
[Responses truncation behavior](https://developers.openai.com/api/reference/resources/responses/methods/create),
[current Python Responses resource](https://github.com/openai/openai-python/blob/b19c2161b1eac80fbf1f6f67a64a50af99c53356/src/openai/resources/responses/responses.py).

## 2. Tool loop and continuation

The official function-calling flow is: send tools, receive a tool call,
execute application code, send the tool output, and receive final text or
more tool calls. The Responses API supports continuing this flow for as many
tool calls as the application allows.

The Python implementation should preserve the complete response output before
adding the function result:

```python
import json

input_items = [{"role": "user", "content": user_text}]

while True:
    response = await client.responses.create(
        model=configured_model,
        instructions=system_prompt,
        input=input_items,
        tools=prepared_tools,
        parallel_tool_calls=False,
        store=False,
        truncation="disabled",
        reasoning={"effort": configured_reasoning_effort},
    )

    # Preserve every item, not only visible assistant text or function calls.
    input_items.extend(item.model_dump() for item in response.output)

    tool_calls = [
        item for item in response.output if item.type == "function_call"
    ]
    if not tool_calls:
        final_text = response.output_text
        break

    # parallel_tool_calls=False is documented to ensure zero or one call.
    if len(tool_calls) != 1:
        raise RuntimeError("unexpected multiple function calls")

    tool_call = tool_calls[0]
    arguments = json.loads(tool_call.arguments)
    result = dispatch_prepared_tool(tool_call.name, arguments)
    input_items.append(
        {
            "type": "function_call_output",
            "call_id": tool_call.call_id,
            "output": json.dumps(result, ensure_ascii=False),
        }
    )
```

The actual runtime must add its configured per-request and per-round limits;
the snippet shows only the API contract. A tool result is normally a string,
and JSON text is suitable for structured local results. The output item must
refer to the exact `call_id` returned by the model. Tool errors should be
returned as deterministic strings so the model can explain them; they should
not become Python exceptions that accidentally skip the continuation item.

With `parallel_tool_calls=False`, the official guide says the model will make
exactly zero or one tool call. Jarvis therefore does not need a tool scheduler
or rollback mechanism. It should nevertheless assert the invariant and log a
provider response that violates it instead of executing several side effects
unexpectedly.

Sources: [function-calling flow and Python continuation example](https://developers.openai.com/api/docs/guides/function-calling#the-tool-calling-flow),
[handling function calls](https://developers.openai.com/api/docs/guides/function-calling#handling-function-calls),
[formatting function results](https://developers.openai.com/api/docs/guides/function-calling#formatting-results),
[parallel function calling](https://developers.openai.com/api/docs/guides/function-calling#parallel-function-calling).

## 3. `store=False` and in-memory stateless history

`store=False` disables stored-response state. Jarvis must therefore send its
history explicitly on every request and must not depend on a retrievable
`previous_response_id` or a Conversations API object.

For a reasoning model, the official stateless-mode guidance is especially
important:

- reasoning items in the response output include an opaque
  `encrypted_content` value by default;
- the application should keep every output item, including encrypted
  reasoning and assistant phase fields;
- the next request should append the next user item and replay the complete
  history;
- the legacy `include=["reasoning.encrypted_content"]` form is accepted for
  compatibility but is not required for this stateless behavior.

The model's private reasoning text is not exposed. Jarvis can trace the
opaque encrypted item exactly, but it cannot log hidden reasoning as readable
text.

Top-level `instructions` are request configuration and are not part of the
response output. Jarvis should send the same system prompt on every request,
including tool-result continuations. This also avoids relying on the separate
rule that `previous_response_id` does not carry top-level instructions.

The in-memory history should consequently contain API-shaped dictionaries for
all of these item kinds:

```text
user message
reasoning output (including encrypted_content when returned)
assistant/message output
function_call output item
function_call_output input item
```

`/new`, inactivity expiry, context overflow, cancellation, or process restart
can discard this list according to the Jarvis session contract. No durable
OpenAI response or conversation ID is needed.

Sources: [stateless reasoning with `store=False`](https://developers.openai.com/api/docs/guides/reasoning#preserve-reasoning-without-stored-responses),
[reasoning items in function-calling history](https://developers.openai.com/api/docs/guides/reasoning#keeping-reasoning-items-in-context),
[Responses state options](https://developers.openai.com/api/docs/guides/migrate-to-responses#3-update-multi-turn-conversations).

## 4. Model and reasoning configuration

`model` is an explicit request field. Jarvis should read it from TOML and pass
it unchanged; it should not silently substitute a different model when the
configured model is unavailable.

`reasoning.effort` is also an explicit request setting. The current official
reasoning guide documents model-dependent values that can include `none`,
`minimal`, `low`, `medium`, `high`, `xhigh`, and `max`. Lower effort generally
reduces latency and token use, while higher effort can improve difficult
multi-step work. The default and supported values are model-dependent, so a
TOML value is not proof that every possible model accepts it. A configuration
or `/reasoning` change should be validated against the selected model, and an
unsupported pair should fail visibly rather than falling back.

The current guide describes GPT-5.6 reasoning mode as `standard` by default
and `pro` as a separate, higher-work option. The simplified Jarvis contract
does not need to expose `reasoning.mode`; pass only the configured effort
unless a later product decision adds a mode.

`reasoning.context` is separate from effort. The guide says GPT-5.6 models
support `all_turns` and use it by default, while earlier models default to
`current_turn`. If implementation exposes this setting, it must be explicit
and model-aware. It should not be inferred from the local transcript alone.
For the minimal initial contract, omitting it lets the selected model's
documented default apply; complete output-item replay remains mandatory in
either case.

Sources: [reasoning model request example](https://developers.openai.com/api/docs/guides/reasoning#get-started-with-reasoning),
[reasoning effort](https://developers.openai.com/api/docs/guides/reasoning#reasoning-effort),
[reasoning mode](https://developers.openai.com/api/docs/guides/reasoning#reasoning-mode),
[reasoning context defaults](https://developers.openai.com/api/docs/guides/reasoning#preserve-reasoning-across-calls).

## 5. Verbatim request and response tracing

The official Python SDK models Responses as Pydantic objects. Its documented
public helpers include `model.to_json()` and `model.to_dict()`; current
function-calling examples also use `response.model_dump_json(indent=2)` and
`item.model_dump()`.

For Jarvis's full trace, the raw HTTP body is the canonical response record,
because it preserves fields without depending on the SDK model's presentation
helpers. The SDK exposes a `.with_raw_response` prefix for every HTTP method.
The returned raw-response wrapper exposes the underlying HTTP request and
response; in the current Python implementation it provides:

```python
raw = await client.responses.with_raw_response.create(**request)

wire_request_body = raw.http_request.content       # bytes
wire_response_body = await raw.text()               # async wrapper
response = await raw.parse()                        # typed Responses object
```

For the synchronous client, `raw.text` and `raw.parse()` are synchronous.
The exact wrapper shape is an SDK-version boundary, so pin the `openai`
version and hide this behind a small Jarvis adapter. The non-raw path is
simpler if only the typed result is required:

```python
response = client.responses.create(**request)
response_json = response.to_json()
```

The raw path is preferable for the stated trace requirement. Before making a
call, also log the logical request dictionary with a deterministic JSON
serializer. After the call, log the actual wire request bytes, status and
non-secret headers, the raw response body, the parsed response fields, and
`retries_taken` when exposed. For a tool turn, log the parsed function name,
arguments, local approval decision, exact tool result, and the continuation
request. This records what Jarvis did with each model output without needing a
separate audit authority.

Do not log the `Authorization` header or other credential-bearing headers.
That is independent of logging the complete user messages, request bodies,
tool results, terminal output, and provider response bodies.

The raw response wrapper captures the final exchange. The SDK's automatic
retry loop does not return every intermediate failed response as a list. If
“everything” includes every transient retry body, the implementation needs an
HTTP transport/event hook that records each attempt, or it must disable SDK
retries and implement an equivalent traced retry loop. With the user's choice
to keep the SDK's normal retries, at minimum log final bodies and caught
`APIStatusError.response` bodies; treat connection and timeout failures as
having no confirmed provider body.

Sources: [Python SDK type and serialization helpers](https://github.com/openai/openai-python/blob/b19c2161b1eac80fbf1f6f67a64a50af99c53356/README.md#using-types),
[function-calling response serialization example](https://developers.openai.com/api/docs/guides/function-calling#function-tool-example),
[raw response interface](https://github.com/openai/openai-python/blob/b19c2161b1eac80fbf1f6f67a64a50af99c53356/README.md#accessing-raw-response-data-eg-headers),
[raw wrapper request/response accessors](https://github.com/openai/openai-python/blob/b19c2161b1eac80fbf1f6f67a64a50af99c53356/src/openai/_legacy_response.py),
[SDK model serialization implementation](https://github.com/openai/openai-python/blob/b19c2161b1eac80fbf1f6f67a64a50af99c53356/src/openai/_models.py).

## 6. Retries, timeouts, and cancellation

The current official Python SDK README documents these defaults:

- requests time out after 10 minutes by default;
- connection errors, HTTP 408, 409, 429, and 5xx responses are retried;
- the default is two retries with short exponential backoff;
- `max_retries` can be set on the client or per request;
- a final timeout raises `APITimeoutError`, while provider status failures
  raise an `APIStatusError` subclass.

The current SDK source confirms that non-GET requests receive an idempotency
key which is reused when that SDK request is retried. The source also honors a
bounded `Retry-After` delay and retries eligible connection/time-out/status
failures. These retries apply to the HTTP Responses request itself. They do
not re-run a local `read_vault` or terminal handler, because those handlers
run only after Jarvis receives and dispatches a function call.

Jarvis has a configured overall request timeout for the whole model/tool loop,
not just one HTTP attempt. A single `responses.create` call can consume its
own timeout and then retry, so the implementation should wrap the whole loop
in an outer deadline and pass the remaining time to each model request. This
keeps the local 10-minute bound meaningful while retaining normal SDK retry
behavior.

For cancellation, the Responses API's `cancel` method is not a general
foreground-request interrupt: the official Python resource documents that
only responses created with `background=True` can be cancelled. Jarvis should
not use background mode for this reactive WhatsApp flow. `/cancel` should
cancel the local async task awaiting `responses.create`, terminate the active
local/SSH subprocess when feasible, mark the request cancelled before stopping
work, and suppress any late result.

Cancelling the local async task closes the client-side wait; it is not proof
that a foreground provider computation already accepted by OpenAI stopped.
The trace should say that the request was locally cancelled and should not
claim a provider-side cancellation or a definite external effect. The same
uncertainty applies if an SSH connection is interrupted after a remote command
may already have started.

Sources: [OpenAI Python retries and timeouts](https://github.com/openai/openai-python/blob/b19c2161b1eac80fbf1f6f67a64a50af99c53356/README.md#retries),
[SDK retry loop and idempotency behavior](https://github.com/openai/openai-python/blob/b19c2161b1eac80fbf1f6f67a64a50af99c53356/src/openai/_base_client.py),
[Responses cancel method](https://github.com/openai/openai-python/blob/b19c2161b1eac80fbf1f6f67a64a50af99c53356/src/openai/resources/responses/responses.py),
[official timeout/retry guidance](https://developers.openai.com/api/docs/guides/flex-processing#api-request-timeouts).

## 7. Explicit `o200k_base` local token gate

The user-selected local counter should be explicit and independent of model
name resolution:

```python
import json
import tiktoken

encoding = tiktoken.get_encoding("o200k_base")


def local_input_tokens(model_facing_request: dict) -> int:
    canonical = json.dumps(
        model_facing_request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(encoding.encode(canonical, disallowed_special=()))
```

The `model_facing_request` should include the fields that contribute to the
model context—at minimum `instructions`, `tools`, and the full `input` list
including the candidate user message and all function-call/tool-output items.
The exact canonical serialization is an application-owned policy and should
be kept stable and unit-tested. `ensure_ascii=False`, sorted keys, compact
separators, and `disallowed_special=()` make the local estimate deterministic
for arbitrary WhatsApp text; the last option tells `tiktoken` to treat text
that resembles a special token as ordinary text rather than raising.

Before each initial or continuation Responses call:

```python
projected = local_input_tokens(
    {
        "instructions": system_prompt,
        "tools": prepared_tools,
        "input": input_items_with_candidate_message,
    }
)
if projected >= config.max_context_tokens:  # 100_000 by default
    send_fixed_context_limit_notice()
    clear_entire_session()
    return
```

The gate is an input-history policy. It does not count future model output,
reasoning tokens generated by the next call, or transport retry time. The
Responses API's `max_output_tokens` limit separately includes visible output
and reasoning tokens, and output usage can exceed visible text because some
models generate non-visible formatting or tool/channel tokens.

The official OpenAI token-counting guide explicitly warns that local
tokenizers such as `tiktoken` work for plain text but do not fully account for
images/files, tool and schema overhead, or model-specific behavior. The same
guide exposes `POST /v1/responses/input_tokens`, which accepts the same payload
as Responses and returns the exact server-side input count. Jarvis is following
the user's simpler `tiktoken` decision rather than adding that extra API call;
therefore “100,000” means the deterministic Jarvis estimate, not a guaranteed
server-reported ceiling. The runtime should still log the provider's returned
`usage.input_tokens` when available so the estimate can be compared during
acceptance.

The official `tiktoken` repository documents `get_encoding("o200k_base")`,
`encoding_for_model(...)`, and the `Encoding.encode(...)` special-token
options. Its model table currently maps the GPT-5 family and several current
reasoning families to `o200k_base`, but the explicit encoding choice avoids a
`KeyError` if a future configured model is not recognized by the local mapping.
That mapping is useful evidence, not a guarantee that server-side structured
request formatting has zero overhead.

Sources: [OpenAI token-counting guide](https://developers.openai.com/api/docs/guides/token-counting),
[exact input-token count API reference](https://developers.openai.com/api/reference/python/resources/responses/subresources/input_tokens/methods/count),
[tiktoken README](https://github.com/openai/tiktoken/blob/4e71bbe0c078468e00fefbf94b39849389f346e5/README.md),
[tiktoken model mapping](https://github.com/openai/tiktoken/blob/4e71bbe0c078468e00fefbf94b39849389f346e5/tiktoken/model.py),
[tiktoken encode and special-token behavior](https://github.com/openai/tiktoken/blob/4e71bbe0c078468e00fefbf94b39849389f346e5/tiktoken/core.py).

## 8. Implementation checks

The direct contract should be covered by focused tests before live OpenWA
acceptance:

1. Every request includes `store=False`, `parallel_tool_calls=False`, the
   configured model, the configured effort when supported, the system prompt,
   and the full API-shaped input history.
2. A fake response containing a reasoning item, a function call, and encrypted
   content is replayed in full; only the one function call is dispatched.
3. The continuation has a `function_call_output` with the exact `call_id` and
   a string result, then the final response text is returned.
4. A malformed response containing multiple function calls is logged and
   rejected without executing multiple actions.
5. Raw request and response bodies, parsed output, tool arguments/results,
   approval decisions, and errors are emitted to the structured trace without
   credentials.
6. The SDK retry behavior is tested with a fake 429/500 and the local outer
   deadline is tested so retries cannot extend the overall request forever.
7. `/cancel` interrupts a foreground async request locally, terminates a
   command subprocess when possible, and suppresses late output; it does not
   claim that OpenAI cancelled a foreground response.
8. The token gate counts instructions, tools, history, and a candidate message
   with the explicit `o200k_base` serializer; exactly 100,000 projected tokens
   ends the session and makes no Responses call.

## 9. Uncertainty and handoff notes

- The eventual TOML model is not specified here. Effort support, reasoning
  context behavior, entitlement, and context limits must be checked against
  that actual model and API project during implementation; the public model
  guide does not prove project access.
- `tiktoken` alone cannot guarantee the exact server input-token count for a
  structured Responses request. This is not a blocker if the product means a
  deterministic local 100,000-token budget. It is a blocker if the product
  instead requires a hard server-side 100,000-token guarantee; that would
  require the input-token-count API or a conservative, explicitly chosen
  margin.
- The Python SDK's public serialization helpers and raw-response wrapper are
  documented, but the raw wrapper is marked as a compatibility surface that
  will change in a future major version. Pin the SDK and put it behind a
  narrow trace adapter.
- The SDK exposes the final raw exchange and retry count, not a complete list
  of every intermediate retry body. Full per-attempt tracing needs transport
  instrumentation if that detail is required by the final trace acceptance.
- Foreground `/cancel` is necessarily best effort at the provider boundary;
  the documented API-level cancellation endpoint applies only to background
  responses, which the simplified runtime does not use.

## Sources

Official OpenAI documentation and first-party sources retrieved on the
research date:

- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [OpenAI reasoning models](https://developers.openai.com/api/docs/guides/reasoning)
- [OpenAI Responses API create reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [OpenAI token counting](https://developers.openai.com/api/docs/guides/token-counting)
- [OpenAI Python SDK at `b19c2161b1eac80fbf1f6f67a64a50af99c53356`](https://github.com/openai/openai-python/commit/b19c2161b1eac80fbf1f6f67a64a50af99c53356)
- [OpenAI tiktoken at `4e71bbe0c078468e00fefbf94b39849389f346e5`](https://github.com/openai/tiktoken/commit/4e71bbe0c078468e00fefbf94b39849389f346e5)
