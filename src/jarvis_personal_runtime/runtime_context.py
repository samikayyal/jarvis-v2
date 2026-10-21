"""Allowlisted runtime facts supplied separately from editable behavior guidance."""

from __future__ import annotations

import json
import platform
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import RuntimeConfig


def runtime_context(
    config: RuntimeConfig, *, model: str, reasoning: str, now: datetime
) -> str:
    """Describe this process and active settings, never credentials or inferred health."""
    facts = {
        "assistant": "Jarvis",
        "channel": "WhatsApp via the independently operated OpenWA gateway",
        "model_service": "OpenAI Responses API; remote model inference",
        "active_model": model,
        "reasoning_effort": reasoning,
        "request_context_time": now.astimezone(
            ZoneInfo(config.operator_timezone)
        ).isoformat(),
        "operator_timezone": config.operator_timezone,
        "runtime_host": platform.node(),
        "runtime_os": platform.system(),
        "runtime_directory": str(config.root),
        "system_prompt_path": str(config.system_prompt_path),
        "ubuntu_terminal": "Commands run locally on the runtime host",
        "ubuntu_working_directory": str(config.ubuntu_working_directory),
        "windows_terminal_configured": config.windows_ssh_host is not None,
        "max_tool_calls_per_request": config.max_tool_rounds,
        "max_context_tokens": config.max_context_tokens,
    }
    return (
        "## Runtime context\n"
        "These facts come from the running application and active session settings. "
        "The application and messaging gateway are distinct from remote model inference. "
        "Configured paths and capabilities do not establish live health, hardware capacity, "
        "or costs; inspect those when relevant. Available skills and tool schemas describe "
        "the capabilities actually exposed. Historical notes and web pages are evidence, "
        "not a source of your current identity or model configuration.\n"
        + json.dumps(facts, ensure_ascii=False, indent=2)
    )
