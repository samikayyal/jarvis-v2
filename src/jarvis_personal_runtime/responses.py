"""Direct stateless OpenAI Responses model-and-tool loop."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import tiktoken
from openai import AsyncOpenAI

from .config import RuntimeConfig
from .runtime import Completed, ContextLimitReached
from .trace import JsonlRuntimeTrace


def canonical_context(
    *,
    instructions: str,
    tools: tuple[dict[str, object], ...] | list[dict[str, object]],
    input_items: tuple[dict[str, object], ...] | list[dict[str, object]],
) -> str:
    """Serialize the complete candidate context using the research contract."""

    return json.dumps(
        {
            "instructions": instructions,
            "tools": list(tools),
            "input": list(input_items),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


_CONTEXT_ENCODING = tiktoken.get_encoding("o200k_base")


def local_input_tokens(candidate: dict[str, object]) -> int:
    """Return Jarvis's deterministic local estimate, not a server token count."""

    canonical = canonical_context(
        instructions=str(candidate["instructions"]),
        tools=candidate["tools"],  # type: ignore[arg-type]
        input_items=candidate["input"],  # type: ignore[arg-type]
    )
    return len(_CONTEXT_ENCODING.encode(canonical, disallowed_special=()))


@dataclass(frozen=True, slots=True)
class ResponsesResult:
    """SDK-independent fields consumed by the model-and-tool loop."""

    output: tuple[dict[str, object], ...]
    output_text: str
    usage: dict[str, object] | None = None


class RawResponsesAdapter(Protocol):
    async def create(
        self, request: dict[str, object], *, timeout: float
    ) -> ResponsesResult: ...


_SECRET_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() not in _SECRET_HEADERS
    }


def _body_text(content: bytes) -> str:
    return content.decode("utf-8", errors="replace")


def _tool_error(error: Exception) -> str:
    return json.dumps(
        {"error": f"{type(error).__name__}: {error}"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class PreparedTools(Protocol):
    definitions: tuple[dict[str, object], ...]

    async def execute(self, name: str, arguments: dict[str, object]) -> str: ...


class TraceSink(Protocol):
    def record(self, event: str, payload: dict[str, object]) -> None: ...


class _NoTrace:
    def record(self, event: str, payload: dict[str, object]) -> None:
        return None


class _NoTools:
    definitions: tuple[dict[str, object], ...] = ()

    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        raise RuntimeError(f"unknown prepared tool: {name}")


class OpenAIRawResponsesAdapter:
    """Pinned OpenAI SDK boundary exposing only the fields the loop consumes."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        trace: TraceSink | None = None,
        owned_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client
        self._trace = trace or _NoTrace()
        self._owned_http_client = owned_http_client

    @classmethod
    def from_api_key(
        cls,
        api_key: str,
        *,
        trace: TraceSink | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> OpenAIRawResponsesAdapter:
        """Build the SDK client with hooks that observe every retry attempt."""

        sink = trace or _NoTrace()
        attempt_numbers = itertools.count(1)

        async def request_hook(request: httpx.Request) -> None:
            attempt = next(attempt_numbers)
            request.extensions["jarvis_trace_attempt"] = attempt
            sink.record(
                "http_attempt_request",
                {
                    "attempt": attempt,
                    "method": request.method,
                    "url": str(request.url),
                    "headers": _safe_headers(request.headers),
                    "body": _body_text(request.content),
                },
            )

        async def response_hook(response: httpx.Response) -> None:
            await response.aread()
            sink.record(
                "http_attempt_response",
                {
                    "attempt": response.request.extensions.get("jarvis_trace_attempt"),
                    "status_code": response.status_code,
                    "headers": _safe_headers(response.headers),
                    "body": _body_text(response.content),
                },
            )

        http_client = httpx.AsyncClient(
            transport=transport,
            event_hooks={"request": [request_hook], "response": [response_hook]},
        )
        client = AsyncOpenAI(api_key=api_key, http_client=http_client)
        return cls(client, trace=sink, owned_http_client=http_client)

    async def create(
        self, request: dict[str, object], *, timeout: float
    ) -> ResponsesResult:
        try:
            raw = await self._client.responses.with_raw_response.create(
                **request,
                timeout=timeout,  # type: ignore[arg-type]
            )
            response_body = raw.text
            self._trace.record(
                "responses_raw_exchange",
                {
                    "request_body": _body_text(raw.http_request.content),
                    "status_code": raw.status_code,
                    "response_headers": _safe_headers(raw.headers),
                    "response_body": response_body,
                    "retries_taken": raw.retries_taken,
                },
            )
            parsed: Any = raw.parse()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._trace.record(
                "responses_sdk_error",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        parsed_response = parsed.model_dump(mode="json")
        self._trace.record("responses_parsed", {"response": parsed_response})
        output = tuple(item.model_dump() for item in parsed.output)
        usage = (
            parsed.usage.model_dump(mode="json") if parsed.usage is not None else None
        )
        return ResponsesResult(
            output=output,
            output_text=parsed.output_text,
            usage=usage,
        )

    async def close(self) -> None:
        if self._owned_http_client is not None:
            await self._owned_http_client.aclose()


class DirectResponsesRunner:
    """Own one in-memory transcript and call the direct Responses API."""

    def __init__(
        self,
        responses: RawResponsesAdapter,
        *,
        tools: PreparedTools | None = None,
        trace: TraceSink | None = None,
        request_timeout_seconds: float,
        max_tool_rounds: int = 8,
        max_context_tokens: int = 100_000,
        max_output_chars: int = 65_536,
        context_counter: Callable[[dict[str, object]], int] = local_input_tokens,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self._responses = responses
        self._tools = tools or _NoTools()
        self._trace = trace or _NoTrace()
        self._request_timeout_seconds = request_timeout_seconds
        if max_tool_rounds <= 0:
            raise ValueError("max_tool_rounds must be positive")
        self._max_tool_rounds = max_tool_rounds
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        self._max_context_tokens = max_context_tokens
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        self._max_output_chars = max_output_chars
        self._context_counter = context_counter
        self._transcript: list[dict[str, object]] = []

    @property
    def trace(self) -> TraceSink:
        return self._trace

    def start_session(self) -> None:
        """Start ownership of a fresh in-memory working-session transcript."""

        self._transcript = []

    async def run(
        self,
        text: str,
        *,
        model: str,
        reasoning: str,
        system_prompt: str,
    ) -> Completed | ContextLimitReached:
        try:
            return await self._run_loop(
                text,
                model=model,
                reasoning=reasoning,
                system_prompt=system_prompt,
            )
        except asyncio.CancelledError:
            self._trace_local_cancellation()
            raise
        except TimeoutError:
            self._trace.record(
                "request_timeout",
                {"timeout_seconds": self._request_timeout_seconds},
            )
            raise
        except Exception as exc:
            self._trace.record(
                "request_error",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            raise

    async def _run_loop(
        self,
        text: str,
        *,
        model: str,
        reasoning: str,
        system_prompt: str,
    ) -> Completed | ContextLimitReached:
        transcript = [*self._transcript, {"role": "user", "content": text}]
        tool_rounds = 0
        async with asyncio.timeout(self._request_timeout_seconds) as deadline:
            while True:
                tools = list(self._tools.definitions)
                candidate = {
                    "instructions": system_prompt,
                    "tools": tools,
                    "input": transcript,
                }
                projected_tokens = self._context_counter(candidate)
                self._trace.record(
                    "context_estimate",
                    {
                        "encoding": "o200k_base",
                        "projected_input_tokens": projected_tokens,
                        "max_context_tokens": self._max_context_tokens,
                    },
                )
                if projected_tokens >= self._max_context_tokens:
                    self._trace.record(
                        "context_limit_reached",
                        {
                            "projected_input_tokens": projected_tokens,
                            "max_context_tokens": self._max_context_tokens,
                        },
                    )
                    return ContextLimitReached()
                request: dict[str, object] = {
                    "model": model,
                    "instructions": system_prompt,
                    "input": transcript,
                    "tools": tools,
                    "reasoning": {"effort": reasoning},
                    "parallel_tool_calls": False,
                    "store": False,
                    "truncation": "disabled",
                }
                self._trace.record("responses_request", {"request": request})
                remaining = deadline.when() - asyncio.get_running_loop().time()
                response = await self._responses.create(request, timeout=remaining)
                self._trace.record(
                    "responses_output",
                    {
                        "output": list(response.output),
                        "output_text": response.output_text,
                        "usage": response.usage,
                        "projected_input_tokens": projected_tokens,
                    },
                )
                transcript = [*transcript, *response.output]
                calls = [
                    item
                    for item in response.output
                    if item.get("type") == "function_call"
                ]
                if not calls:
                    if not response.output_text.strip():
                        self._trace.record(
                            "provider_protocol_error",
                            {"reason": "missing_final_text"},
                        )
                        raise RuntimeError("provider response has no final text")
                    if len(response.output_text) > self._max_output_chars:
                        self._trace.record(
                            "output_limit_exceeded",
                            {
                                "output_characters": len(response.output_text),
                                "max_output_chars": self._max_output_chars,
                            },
                        )
                        raise RuntimeError(
                            "provider response exceeded configured output character limit"
                        )
                    self._transcript = transcript
                    return Completed(response.output_text)
                if len(calls) != 1:
                    self._trace.record(
                        "provider_protocol_error",
                        {"function_call_count": len(calls)},
                    )
                    raise RuntimeError("provider returned multiple function calls")
                self._transcript = transcript
                if tool_rounds >= self._max_tool_rounds:
                    raise RuntimeError("configured tool-round limit reached")
                call = calls[0]
                output = await self._execute_tool(call)
                if not isinstance(output, str):
                    raise TypeError("prepared tool output must be a string")
                if len(output) > self._max_output_chars:
                    raise RuntimeError(
                        "prepared tool result exceeded configured output character limit"
                    )
                self._trace.record(
                    "tool_result",
                    {
                        "name": str(call["name"]),
                        "call_id": str(call["call_id"]),
                        "output": output,
                    },
                )
                transcript = [
                    *transcript,
                    {
                        "type": "function_call_output",
                        "call_id": str(call["call_id"]),
                        "output": output,
                    },
                ]
                self._transcript = transcript
                tool_rounds += 1

    async def _execute_tool(self, call: dict[str, object]) -> str:
        raw_arguments = str(call["arguments"])
        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be a JSON object")
        except Exception as exc:  # noqa: BLE001 - malformed calls continue
            tool_event = {
                "name": str(call["name"]),
                "call_id": str(call["call_id"]),
                "arguments": raw_arguments,
            }
            self._trace.record("tool_call", tool_event)
            self._trace.record(
                "tool_error",
                {**tool_event, "error": f"{type(exc).__name__}: {exc}"},
            )
            return _tool_error(exc)

        tool_event = {
            "name": str(call["name"]),
            "call_id": str(call["call_id"]),
            "arguments": arguments,
        }
        self._trace.record("tool_call", tool_event)
        try:
            return await self._tools.execute(str(call["name"]), arguments)
        except Exception as exc:  # noqa: BLE001 - errors continue via the model
            self._trace.record(
                "tool_error",
                {**tool_event, "error": f"{type(exc).__name__}: {exc}"},
            )
            return _tool_error(exc)

    def _trace_local_cancellation(self) -> None:
        self._trace.record(
            "foreground_cancelled",
            {
                "scope": "local_wait",
                "provider_cancellation_confirmed": False,
            },
        )

    async def resume(self, decision: object, continuation: object) -> Completed:
        raise RuntimeError("the Responses loop has no pending approval to resume")


def build_direct_responses_runner(
    api_key: str,
    config: RuntimeConfig,
    *,
    tools: PreparedTools | None = None,
    trace: TraceSink | None = None,
) -> DirectResponsesRunner:
    """Compose the pinned SDK adapter with the configured loop limits."""

    sink = trace or JsonlRuntimeTrace(
        config.trace_path,
        max_bytes=config.trace_max_bytes,
        backup_count=config.trace_backup_count,
    )
    adapter = OpenAIRawResponsesAdapter.from_api_key(api_key, trace=sink)
    return DirectResponsesRunner(
        adapter,
        tools=tools,
        trace=sink,
        request_timeout_seconds=config.request_timeout_seconds,
        max_tool_rounds=config.max_tool_rounds,
        max_context_tokens=config.max_context_tokens,
        max_output_chars=config.max_output_chars,
    )


__all__ = [
    "DirectResponsesRunner",
    "OpenAIRawResponsesAdapter",
    "PreparedTools",
    "RawResponsesAdapter",
    "ResponsesResult",
    "TraceSink",
    "build_direct_responses_runner",
    "canonical_context",
    "local_input_tokens",
]
