# ruff: noqa: F401, PLE0605 -- compatibility exports are intentional.
"""Bounded OpenAI Agents SDK adapter for non-authoritative planning."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from threading import Event, Lock
from time import monotonic
from typing import Any

from ...models import (
    OrchestrationMilestone,
    OrchestrationRequest,
    OrchestrationResult,
)
from ...ports import OrchestrationAdapterError
from ...proposal_translation import (
    AgentsSdkPlan,
    AgentsSdkProposal,
    _AgentsSdkStructuredPlan,
    build_instructions,
    translate_plan,
)
from . import (
    BoundedReadInput,
    BoundedReadOutput,
    BoundedReadTool,
    _excluded_capability_refusal,
    _safe_unavailable_read_reason,
    _ToolInvocationBudget,
    _unavailable_read_reply,
)
from .read_tools import (
    _GOOGLE_CONTENT_UNAVAILABLE_REASONS,
    _READ_UNAVAILABLE_RESULT,
)
from .support import (
    _ADAPTER_EXPORTS,
    _ActiveModelTurn,
    _default_read_tool,
    _model_input_with_history,
    _ModelTurnCancelled,
    _ModelTurnDeadlineExceeded,
    _stale_vault_disclosure,
)

_instructions = build_instructions
__all__ = _ADAPTER_EXPORTS

_MAX_TURNS = 5
_MAX_TOOL_INVOCATIONS = 4
_MODEL_CANCELLATION_GRACE_SECONDS = 5.0


class AgentsSdkOrchestrationAdapter:
    """Responses-backed planner constrained to one stateless, sequential run."""

    def __init__(
        self,
        *,
        agent_factory: Callable[..., Any] | None = None,
        run_sync: Callable[..., Any] | None = None,
        run_async: Callable[..., Any] | None = None,
        model_settings_factory: Callable[..., Any] | None = None,
        reasoning_factory: Callable[..., Any] | None = None,
        run_config_factory: Callable[..., Any] | None = None,
        max_turns: int = _MAX_TURNS,
        max_tool_invocations: int = _MAX_TOOL_INVOCATIONS,
        read_tool: BoundedReadTool | None = None,
        google_read_connector: object | None = None,
        vault_read_tool: BoundedReadTool | None = None,
        vault_write_enabled: bool = False,
        model_turn_timeout_seconds: float | None = None,
    ) -> None:
        if (
            isinstance(max_turns, bool)
            or not isinstance(max_turns, int)
            or not 1 <= max_turns <= _MAX_TURNS
        ):
            raise ValueError(f"max_turns must be between 1 and {_MAX_TURNS}")
        if (
            isinstance(max_tool_invocations, bool)
            or not isinstance(max_tool_invocations, int)
            or not 1 <= max_tool_invocations <= _MAX_TOOL_INVOCATIONS
        ):
            raise ValueError(
                f"max_tool_invocations must be between 1 and {_MAX_TOOL_INVOCATIONS}"
            )
        if not isinstance(vault_write_enabled, bool):
            raise TypeError("vault_write_enabled must be a bool")
        if run_sync is not None and run_async is not None:
            raise ValueError("provide only one Agents SDK runner")
        if model_turn_timeout_seconds is not None and (
            isinstance(model_turn_timeout_seconds, bool)
            or not isinstance(model_turn_timeout_seconds, (int, float))
            or model_turn_timeout_seconds <= 0
        ):
            raise ValueError("model turn timeout must be positive")
        if any(
            value is None
            for value in (
                agent_factory,
                model_settings_factory,
                reasoning_factory,
                run_config_factory,
            )
        ) or (run_sync is None and run_async is None):
            from agents import Agent, ModelSettings, RunConfig, Runner
            from openai.types.shared import Reasoning

            agent_factory = agent_factory or Agent
            if run_sync is None and run_async is None:
                run_async = Runner.run
            model_settings_factory = model_settings_factory or ModelSettings
            reasoning_factory = reasoning_factory or Reasoning
            run_config_factory = run_config_factory or RunConfig
        self._agent_factory = agent_factory
        self._run_sync = run_sync
        self._run_async = run_async
        self._model_settings_factory = model_settings_factory
        self._reasoning_factory = reasoning_factory
        self._run_config_factory = run_config_factory
        self._max_turns = max_turns
        self._max_tool_invocations = max_tool_invocations
        read_tools = []
        if read_tool is not None:
            if not isinstance(read_tool, BoundedReadTool):
                raise TypeError("read_tool must be a BoundedReadTool")
            if read_tool.name != "read_request_context":
                raise ValueError("read_tool can replace only read_request_context")
            read_tools.append(read_tool)
        else:
            read_tools.append(_default_read_tool())
        if google_read_connector is not None:
            # Only the connector factory may introduce Google read handlers.
            from ...google_reads import _google_read_tools

            read_tools.extend(_google_read_tools(google_read_connector))
        if vault_read_tool is not None and not isinstance(
            vault_read_tool, BoundedReadTool
        ):
            raise TypeError("vault_read_tool must be a BoundedReadTool")
        if (
            vault_read_tool is not None
            and vault_read_tool.name != "read_knowledge_vault"
        ):
            raise ValueError("vault read tool is outside the closed tool set")
        if vault_read_tool is not None:
            read_tools.append(vault_read_tool)
        self._read_tools = tuple(read_tools)
        self._vault_write_enabled = vault_write_enabled
        self._model_turn_timeout_seconds = model_turn_timeout_seconds
        if self._run_sync is not None and model_turn_timeout_seconds is not None:
            raise ValueError(
                "model turn timeout requires the cancellable async Agents SDK runner"
            )
        self._cancellation_lock = Lock()
        self._cancelled_requests: set[str] = set()
        self._active_model_turns: dict[str, _ActiveModelTurn] = {}

    def run(self, request: OrchestrationRequest) -> OrchestrationResult:
        try:
            return self._run(request)
        finally:
            with self._cancellation_lock:
                self._cancelled_requests.discard(request.state.request_id)

    def _run(self, request: OrchestrationRequest) -> OrchestrationResult:
        milestones = [
            OrchestrationMilestone(
                stage="orchestration_started",
                message="Started bounded orchestration.",
            )
        ]
        excluded_refusal = _excluded_capability_refusal(request.text)
        if excluded_refusal is not None:
            milestones.append(
                OrchestrationMilestone(
                    stage="excluded_capability_refused",
                    message="Refused a capability excluded from Jarvis v1.",
                )
            )
            return OrchestrationResult(
                request_id=request.state.request_id,
                outcome="completed",
                reply_text=excluded_refusal,
                adapter="agents_sdk_responses",
                milestones=tuple(milestones),
            )
        budget = _ToolInvocationBudget(self._max_tool_invocations)
        stale_vault_read: tuple[datetime, str] | None = None
        unavailable_reads: list[tuple[str, str]] = []

        def record_stale_vault_read(synchronized_at: datetime, warning: str) -> None:
            nonlocal stale_vault_read
            if stale_vault_read is None:
                stale_vault_read = (synchronized_at, warning)

        try:
            from agents import AgentOutputSchema

            tools = self._build_tools(
                request,
                milestones,
                budget,
                unavailable_reads,
                record_stale_vault_read=record_stale_vault_read,
            )
            agent = self._agent_factory(
                name="Jarvis orchestration agent",
                instructions=build_instructions(
                    has_vault_read=any(
                        tool.name == "read_knowledge_vault" for tool in self._read_tools
                    ),
                    has_vault_write=self._vault_write_enabled,
                ),
                model=request.model,
                model_settings=self._model_settings_factory(
                    reasoning=self._reasoning_factory(effort=request.reasoning),
                    parallel_tool_calls=False,
                    store=False,
                ),
                tools=tools,
                output_type=AgentOutputSchema(
                    _AgentsSdkStructuredPlan, strict_json_schema=False
                ),
            )
            run_config = self._run_config_factory(
                tracing_disabled=True,
                trace_include_sensitive_data=False,
            )
            model_input = _model_input_with_history(request)
            run_result = self._run_model_turn(
                request=request,
                agent=agent,
                model_input=model_input,
                run_config=run_config,
            )
        except OrchestrationAdapterError:
            raise
        except Exception as exc:
            raise OrchestrationAdapterError("Agents SDK run was unavailable") from exc

        if unavailable_reads:
            tool_name, reason = unavailable_reads[0]
            return OrchestrationResult(
                request_id=request.state.request_id,
                outcome="unavailable",
                reply_text=_unavailable_read_reply(tool_name, reason),
                adapter="agents_sdk_responses",
                milestones=tuple(milestones),
            )

        raw_plan = getattr(run_result, "final_output", None)
        translation = translate_plan(request, raw_plan)
        reply_text = translation.reply_text
        if stale_vault_read is not None:
            reply_text += _stale_vault_disclosure(*stale_vault_read)
        return OrchestrationResult(
            request_id=request.state.request_id,
            outcome="completed",
            reply_text=reply_text,
            adapter="agents_sdk_responses",
            proposal=translation.proposal,
            proposal_intent=translation.proposal_intent,
            execution_host=translation.execution_host,
            host_reason_code=translation.host_reason_code,
            milestones=tuple(milestones),
        )

    def _run_model_turn(
        self,
        *,
        request: OrchestrationRequest,
        agent: object,
        model_input: object,
        run_config: object,
    ) -> object:
        kwargs = {
            "max_turns": self._max_turns,
            "run_config": run_config,
            "previous_response_id": None,
            "auto_previous_response_id": False,
            "conversation_id": None,
        }
        if self._run_async is None:
            if self._run_sync is None:
                raise OrchestrationAdapterError("Agents SDK runner is unavailable")
            return self._run_sync(agent, model_input, **kwargs)

        async def run_bounded() -> object:
            operation = self._run_async(agent, model_input, **kwargs)
            task = asyncio.ensure_future(operation)
            active = _ActiveModelTurn(
                loop=asyncio.get_running_loop(),
                task=task,
                quiesced=Event(),
            )
            request_id = request.state.request_id
            with self._cancellation_lock:
                if request_id in self._active_model_turns:
                    task.cancel()
                    raise OrchestrationAdapterError(
                        "request already has an active Agents SDK model turn"
                    )
                self._active_model_turns[request_id] = active
                cancel_immediately = request_id in self._cancelled_requests
            if cancel_immediately:
                task.cancel()
            try:
                if self._model_turn_timeout_seconds is None:
                    return await task
                done, _pending = await asyncio.wait(
                    (task,), timeout=self._model_turn_timeout_seconds
                )
                if task not in done:
                    task.cancel()
                    done, _pending = await asyncio.wait(
                        (task,), timeout=_MODEL_CANCELLATION_GRACE_SECONDS
                    )
                    if task not in done:
                        task.cancel()
                        raise OrchestrationAdapterError(
                            "Agents SDK model turn did not establish quiescence"
                        )
                    raise _ModelTurnDeadlineExceeded
                return await task
            except asyncio.CancelledError as exc:
                if self._request_is_cancelled(request_id):
                    raise _ModelTurnCancelled from exc
                raise
            finally:
                with self._cancellation_lock:
                    if self._active_model_turns.get(request_id) is active:
                        self._active_model_turns.pop(request_id, None)
                if task.done():
                    active.quiesced.set()

        try:
            return asyncio.run(run_bounded())
        except _ModelTurnDeadlineExceeded as exc:
            self.cancel(request_id=request.state.request_id)
            raise OrchestrationAdapterError(
                "Agents SDK model turn exceeded its configured deadline"
            ) from exc
        except _ModelTurnCancelled as exc:
            raise OrchestrationAdapterError(
                "Agents SDK model turn was cancelled"
            ) from exc

    def cancel(self, *, request_id: str) -> bool:
        """Cancel an active model turn only after model-task quiescence."""

        with self._cancellation_lock:
            self._cancelled_requests.add(request_id)
            active = self._active_model_turns.get(request_id)
            if active is not None:
                try:
                    active.loop.call_soon_threadsafe(active.task.cancel)
                except RuntimeError as exc:
                    raise OrchestrationAdapterError(
                        "active Agents SDK model turn could not be cancelled"
                    ) from exc
        if active is not None and not active.quiesced.wait(
            timeout=_MODEL_CANCELLATION_GRACE_SECONDS
        ):
            raise OrchestrationAdapterError(
                "active Agents SDK model turn did not establish quiescence"
            )
        return active is not None

    def _request_is_cancelled(self, request_id: str) -> bool:
        with self._cancellation_lock:
            return request_id in self._cancelled_requests

    def _build_tools(
        self,
        request: OrchestrationRequest,
        milestones: list[OrchestrationMilestone],
        budget: _ToolInvocationBudget,
        unavailable_reads: list[tuple[str, str]],
        record_stale_vault_read: Callable[[datetime, str], None],
    ) -> list[Any]:
        """Build the closed tool list anew for each request and invocation budget."""

        from agents import FunctionTool

        tools: list[Any] = []
        for read_tool in self._read_tools:

            async def invoke(
                _context: Any, raw_input: str, *, tool: BoundedReadTool = read_tool
            ) -> object:
                if unavailable_reads:
                    return _READ_UNAVAILABLE_RESULT
                budget.consume()
                try:
                    typed_input = tool.input_model.model_validate_json(raw_input)
                    deadline = monotonic() + tool.timeout_seconds
                    typed_output = await asyncio.wait_for(
                        asyncio.to_thread(tool.handler, request, typed_input, deadline),
                        timeout=tool.timeout_seconds,
                    )
                    if not isinstance(typed_output, tool.output_model):
                        raise TypeError("bounded read returned an untyped result")
                    bounded_output = tool.output_model.model_validate(typed_output)
                    if tool.name == "read_knowledge_vault":
                        warning = getattr(bounded_output, "stale_warning", None)
                        synchronized_at = getattr(
                            bounded_output, "synchronized_at", None
                        )
                        if isinstance(warning, str) and isinstance(
                            synchronized_at, datetime
                        ):
                            record_stale_vault_read(synchronized_at, warning)
                    if (
                        tool.name == "read_google_drive"
                        and getattr(bounded_output, "content_available", None) is False
                    ):
                        content_reason = _GOOGLE_CONTENT_UNAVAILABLE_REASONS.get(
                            getattr(
                                bounded_output,
                                "content_unavailable_reason",
                                None,
                            )
                        )
                        if content_reason is None:
                            raise OrchestrationAdapterError(
                                "Google Drive returned an unsupported content result"
                            )
                        unavailable_reads.append((tool.name, content_reason))
                        milestones.append(
                            OrchestrationMilestone(
                                stage="bounded_read_unavailable",
                                message=(
                                    "Bounded read with read_google_drive reported "
                                    "content unavailable."
                                ),
                            )
                        )
                        return _READ_UNAVAILABLE_RESULT
                except TimeoutError:
                    unavailable_reason = "the service timed out"
                    unavailable_reads.append((tool.name, unavailable_reason))
                    milestones.append(
                        OrchestrationMilestone(
                            stage="bounded_read_unavailable",
                            message=f"Bounded read with {tool.name} was unavailable.",
                        )
                    )
                    return _READ_UNAVAILABLE_RESULT
                except Exception as exc:
                    unavailable_reason = _safe_unavailable_read_reason(exc)
                    if unavailable_reason is None:
                        if isinstance(exc, OrchestrationAdapterError):
                            raise
                        raise OrchestrationAdapterError(
                            "bounded read tool returned malformed data"
                        ) from exc
                    unavailable_reads.append((tool.name, unavailable_reason))
                    milestones.append(
                        OrchestrationMilestone(
                            stage="bounded_read_unavailable",
                            message=f"Bounded read with {tool.name} was unavailable.",
                        )
                    )
                    return _READ_UNAVAILABLE_RESULT
                milestones.append(
                    OrchestrationMilestone(
                        stage="bounded_read",
                        message=f"Completed bounded read with {tool.name}.",
                    )
                )
                bounded_result = bounded_output.model_dump(mode="json")
                if tool.name in {
                    "read_gmail",
                    "read_google_drive",
                }:
                    bounded_result = {
                        key: value
                        for key, value in bounded_result.items()
                        if value is not None
                    }
                return bounded_result

            tools.append(
                FunctionTool(
                    name=read_tool.name,
                    description=read_tool.description,
                    params_json_schema=read_tool.input_model.model_json_schema(),
                    on_invoke_tool=invoke,
                    strict_json_schema=True,
                    needs_approval=False,
                    timeout_seconds=read_tool.timeout_seconds,
                    output_json_schema=read_tool.output_model.model_json_schema(),
                )
            )
        return tools
