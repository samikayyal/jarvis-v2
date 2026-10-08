"""Opt-in installed-wheel smoke test; no model, messaging, or credentials used.

Run outside the source checkout with `uv run --no-project --python ... python
verify-web.py --live --output evidence.json`. Live outcomes are observations,
not assertions that Google always works. The provider is deliberately scripted.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.resources
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from jarvis_personal_runtime.config import WebSearchConfig
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
        output=(
            {
                "type": "function_call",
                "name": name,
                "call_id": name,
                "arguments": json.dumps(arguments),
            },
        ),
        output_text="",
    )


class ScriptedProvider:
    """Exercise the installed model/tool loop without billing a model service."""

    def __init__(self) -> None:
        self.steps = []
        self.evidence = []
        self.processed_outputs = 0
        self.request_output_start = 0
        self.reuse_session = False
        self.supporting_page = None

    async def create(self, request: dict, *, timeout: float) -> ResponsesResult:
        outputs = [
            item
            for item in request["input"]
            if item.get("type") == "function_call_output"
        ]
        for item in outputs[self.processed_outputs :]:
            result = json.loads(item["output"])
            if "outcome" not in result:
                continue
            self.evidence.append(result)
        self.processed_outputs = len(outputs)
        if self.steps:
            return self.steps.pop(0)
        available = (
            outputs if self.reuse_session else outputs[self.request_output_start :]
        )
        self.supporting_page = None
        for item in available:
            for page in json.loads(item["output"]).get("pages", []):
                if page.get("outcome") == "ok" and page.get("text"):
                    self.supporting_page = page
        reply = "No verified source text is available."
        if self.supporting_page is not None:
            page = self.supporting_page
            reply = (
                f"The source says: {page['text'][:240]} [1]\n\n"
                f"[1] {page['title']}: {page['url']}"
            )
        return ResponsesResult(output=(), output_text=reply)


async def verify(live: bool, report: dict) -> None:
    if live and os.name != "posix":
        raise RuntimeError(
            "live worker verification requires the supported POSIX environment"
        )
    package = importlib.resources.files("jarvis_personal_runtime")
    skill = package.joinpath("skills/web/SKILL.md").read_text(encoding="utf-8")
    assert "search_web" in skill and "read_web_pages" in skill
    dependencies = importlib.metadata.requires("jarvis-v2")
    assert dependencies and all("==" in item for item in dependencies)
    report.update(
        {
            "package": str(package),
            "dependencies": dependencies,
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "provider": "scripted; no live-model or WhatsApp acceptance",
            "binaries": {},
        }
    )
    for name, path in (("browser", find_chromium()), ("driver", find_chromedriver())):
        version = None
        if path and os.name == "posix":
            version = (
                await asyncio.to_thread(
                    subprocess.check_output, [path, "--version"], text=True, timeout=10
                )
            ).strip()
        report["binaries"][name] = {
            "path": path,
            "version": version,
        }
    if not live:
        return
    with tempfile.TemporaryDirectory(prefix="jarvis-web-verification-") as temporary:
        root = Path(temporary)
        provider = ScriptedProvider()
        report["evidence"] = provider.evidence
        tools = SkillTools(
            PreparedToolCollection(
                WebTools(
                    WebSearchConfig(enabled=True),
                    runtime_dir=root,
                    max_output_chars=65536,
                )
            )
        )
        runner = DirectResponsesRunner(
            provider, tools=tools, request_timeout_seconds=120
        )

        async def run(label: str, text: str, steps: list) -> None:
            provider.steps = steps
            provider.request_output_start = provider.processed_outputs
            provider.reuse_session = not steps
            answer = await runner.run(
                text,
                model="scripted",
                reasoning="medium",
                system_prompt="Use cited evidence.",
            )
            report[label] = answer.reply
            report["profile_cleanup"] = not list(root.iterdir())
            assert report["profile_cleanup"], "request left a browser profile behind"

        await run(
            "search_reply",
            "Search for the IANA example domains documentation",
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
            "Read https://www.iana.org/help/example-domains",
            [
                tool_call(
                    "read_web_pages",
                    {
                        "urls": ["https://www.iana.org/help/example-domains"],
                        "depth": "quick",
                    },
                ),
            ],
        )
        count = len(provider.evidence)
        original_page = provider.supporting_page
        await run("follow_up_reply", "Repeat the source evidence from this session", [])
        assert len(provider.evidence) == count
        assert provider.supporting_page == original_page
        report["follow_up_source_in_transcript"] = provider.supporting_page is not None
        await run(
            "recheck_reply",
            "Recheck that page now",
            [
                tool_call(
                    "read_web_pages",
                    {
                        "urls": ["https://www.iana.org/help/example-domains"],
                        "depth": "quick",
                    },
                ),
            ],
        )
        count = len(provider.evidence)
        rechecked_page = provider.supporting_page
        await run(
            "post_recheck_follow_up_reply", "Repeat the rechecked source evidence", []
        )
        assert len(provider.evidence) == count
        if rechecked_page is not None:
            assert provider.supporting_page == rechecked_page
        report["post_recheck_source_in_transcript"] = (
            provider.supporting_page is not None
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    observations = {}
    try:
        asyncio.run(verify(args.live, observations))
    except Exception as exc:  # noqa: BLE001 - preserve failed probe evidence.
        observations["verification_error"] = f"{type(exc).__name__}: {exc}"
    args.output.write_text(json.dumps(observations, indent=2), encoding="utf-8")
    print(f"Wrote observations to {args.output}")
    return 1 if "verification_error" in observations else 0


if __name__ == "__main__":
    sys.exit(main())
