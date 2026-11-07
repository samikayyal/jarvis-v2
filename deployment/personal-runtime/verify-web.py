"""Installed-wheel browser smoke probe with scripted providers, no keys/messages.

Run from the locked installed environment with --live for actual public browser
access. This is provisioning evidence, not model accuracy or WhatsApp acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.resources
import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from jarvis_personal_runtime.config import WebReaderConfig, WebSearchConfig
from jarvis_personal_runtime.playwright_reader import PlaywrightReaderTools
from jarvis_personal_runtime.responses import (
    DirectResponsesRunner,
    PreparedToolCollection,
    ResponsesResult,
)
from jarvis_personal_runtime.skill_tools import SkillTools
from jarvis_personal_runtime.web_tools import WebTools
from jarvis_personal_runtime.web_worker import find_chromedriver, find_chromium


def tool_call(name: str, arguments: dict) -> ResponsesResult:
    return ResponsesResult(
        (
            {
                "type": "function_call",
                "name": name,
                "call_id": uuid4().hex,
                "arguments": json.dumps(arguments),
            },
        ),
        "",
    )


class ScriptedProvider:
    """Read findings from actual tool outputs, with no private provider memory."""

    def __init__(self):
        self.steps = []
        self.evidence = []
        self.processed_outputs = 0
        self.request_output_start = 0
        self.reuse_session = False
        self.supporting_findings = None
        self.calls = 0

    async def create(self, request: dict, *, timeout: float) -> ResponsesResult:
        self.calls += 1
        outputs = [
            i for i in request["input"] if i.get("type") == "function_call_output"
        ]
        for item in outputs[self.processed_outputs :]:
            result = json.loads(item["output"])
            if "outcome" in result or "state" in result:
                self.evidence.append(result)
        self.processed_outputs = len(outputs)
        if self.steps:
            step = self.steps.pop(0)
            return step(outputs) if callable(step) else step
        available = (
            outputs if self.reuse_session else outputs[self.request_output_start :]
        )
        self.supporting_findings = None
        latest = None
        for item in available:
            result = json.loads(item["output"])
            if "state" in result:
                latest = result
        if latest and latest["state"] == "running":
            return tool_call(
                "wait_browser_reader", {"reader_id": latest["reader_id"], "seconds": 30}
            )
        if latest and latest["state"] == "completed" and latest.get("findings"):
            self.supporting_findings = latest["findings"]
        return ResponsesResult(
            (), self.supporting_findings or "No source findings are available."
        )


class ReaderProvider:
    """Return a literal initial-page excerpt; does not judge semantic correctness."""

    def __init__(self):
        self.calls = 0

    async def create(self, request, *, timeout):
        self.calls += 1
        observation = json.loads(request["input"][-1]["content"])
        return ResponsesResult(
            (),
            "Observed excerpt: "
            + observation.get("text", "")[:240]
            + " Source: "
            + observation.get("url", "")
            + ". Scripted probe; no model accuracy claim.",
        )


async def verify(live: bool, report: dict) -> None:
    if live and os.name != "posix":
        raise RuntimeError(
            "live worker verification requires the supported POSIX environment"
        )
    package = importlib.resources.files("jarvis_personal_runtime")
    skill = package.joinpath("skills/web/SKILL.md").read_text(encoding="utf-8")
    assert "search_web" in skill and "start_browser_reader" in skill
    assert "ask_page_reader" not in skill
    dependencies = importlib.metadata.requires("jarvis-v2")
    assert dependencies and all("==" in item for item in dependencies)
    report.update(
        package=str(package),
        dependencies=dependencies,
        uid=os.getuid() if hasattr(os, "getuid") else None,
        provider="scripted; no keys, model accuracy or WhatsApp acceptance",
        binaries={"browser": find_chromium(), "driver": find_chromedriver()},
    )
    if not live:
        return
    with tempfile.TemporaryDirectory(prefix="jarvis-web-verification-") as temporary:
        root = Path(temporary)
        provider, reader_provider = ScriptedProvider(), ReaderProvider()
        readers = PlaywrightReaderTools(WebReaderConfig(), reader_provider)
        report["evidence"] = provider.evidence
        tools = SkillTools(
            PreparedToolCollection(
                WebTools(
                    WebSearchConfig(enabled=True),
                    runtime_dir=root,
                    max_output_chars=65536,
                ),
                readers,
            )
        )
        runner = DirectResponsesRunner(
            provider, tools=tools, request_timeout_seconds=120, max_tool_rounds=12
        )

        async def run(label, steps):
            provider.steps = steps
            provider.request_output_start = provider.processed_outputs
            provider.reuse_session = not steps
            started = asyncio.get_running_loop().time()
            answer = await runner.run(
                label,
                model="scripted",
                reasoning="low",
                system_prompt="Use actual source findings.",
            )
            report[label] = answer.reply
            report[label + "_seconds"] = asyncio.get_running_loop().time() - started
            report["profile_cleanup"] = not list(root.iterdir())
            assert report["profile_cleanup"], "search profile left behind"
            for reader_id in list(readers.readers):
                state = json.loads(
                    await readers.execute(
                        "browser_reader_status", {"reader_id": reader_id}
                    )
                )
                assert state["browser_state"] == "closed", state
            report["reader_browser_cleanup"] = True

        await run(
            "search_reply",
            [
                tool_call("load_skill", {"name": "web"}),
                tool_call(
                    "search_web",
                    {"query": "site:iana.org example domains", "depth": "quick"},
                ),
            ],
        )
        await run(
            "page_reply",
            [
                tool_call(
                    "start_browser_reader",
                    {
                        "url": "https://www.iana.org/help/example-domains",
                        "prompt": "Report a literal excerpt and its source URL.",
                    },
                )
            ],
        )
        original = provider.supporting_findings
        reader_id = next(iter(readers.readers))
        count = reader_provider.calls
        await run("follow_up_reply", [])
        assert (
            reader_provider.calls == count and provider.supporting_findings == original
        )
        await run(
            "recheck_reply",
            [
                tool_call(
                    "follow_up_browser_reader",
                    {
                        "reader_id": reader_id,
                        "prompt": "Reopen the source and report a fresh literal excerpt.",
                    },
                )
            ],
        )
        assert reader_provider.calls > count
        report["scripted_main_calls"] = provider.calls
        report["scripted_reader_calls"] = reader_provider.calls
        await readers.close_request()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    observations = {}
    try:
        asyncio.run(verify(args.live, observations))
    except Exception as exc:  # noqa: BLE001 - preserve failed probe evidence
        observations["verification_error"] = f"{type(exc).__name__}: {exc}"
    args.output.write_text(json.dumps(observations, indent=2), encoding="utf-8")
    print(f"Wrote observations to {args.output}")
    return 1 if "verification_error" in observations else 0


if __name__ == "__main__":
    sys.exit(main())
