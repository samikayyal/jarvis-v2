"""Reader lifecycle and provider payloads tested through main prepared tools."""

import asyncio
import copy
import json
import os
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from openai import BadRequestError

from jarvis_personal_runtime.config import WebReaderConfig
from jarvis_personal_runtime.playwright_reader import PlaywrightReaderTools
from jarvis_personal_runtime.responses import (
    DirectResponsesRunner,
    OpenAIRawResponsesAdapter,
    PreparedToolCollection,
    ResponsesResult,
)
from jarvis_personal_runtime.runtime import Completed
from jarvis_personal_runtime.skill_tools import SkillTools

from .test_playwright_browser import public_fixture


def call(tool_name, **arguments):
    return ResponsesResult(
        (
            {
                "type": "function_call",
                "name": tool_name,
                "call_id": "call-" + str(id(arguments)),
                "arguments": json.dumps(arguments),
            },
        ),
        "",
    )


class ScriptedModel:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []

    async def create(self, request, *, timeout):
        self.requests.append(copy.deepcopy(request))
        step = next(self.steps)
        return step(request) if callable(step) else step


async def finished(tools, url, prompt="Investigate the public fixture"):
    start = json.loads(
        await tools.execute("start_browser_reader", {"url": url, "prompt": prompt})
    )
    return json.loads(
        await tools.execute(
            "wait_browser_reader", {"reader_id": start["reader_id"], "seconds": 30}
        )
    )


class ControlledModel:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def create(self, request, *, timeout):
        self.requests.append(copy.deepcopy(request))
        self.started.set()
        await self.release.wait()
        return ResponsesResult(
            (),
            "The public calculator lists the small machine at 2. Source: "
            + json.loads(request["input"][1]["content"])["url"],
        )


def test_main_can_work_before_collecting_ordinary_written_findings(monkeypatch):
    async def scenario(url):
        model = ControlledModel()
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        tools.request_started()
        try:
            start = json.loads(
                await tools.execute(
                    "start_browser_reader", {"url": url, "prompt": "Check the price."}
                )
            )
            assert start["state"] == "running"
            await asyncio.wait_for(model.started.wait(), 15)
            status = json.loads(
                await tools.execute(
                    "browser_reader_status", {"reader_id": start["reader_id"]}
                )
            )
            assert status["state"] == "running"
            # Observable independent main work while the provider boundary is held.
            other_work = 6 * 7
            assert other_work == 42
            model.release.set()
            result = json.loads(
                await tools.execute(
                    "wait_browser_reader",
                    {"reader_id": start["reader_id"], "seconds": 15},
                )
            )
            assert result["state"] == "completed"
            assert "small machine at 2" in result["findings"]
            assert url in result["findings"]
            assert "coverage" not in result and "evidence" not in result
            assert "# Public calculator" in model.requests[0]["input"][1]["content"]
            assert model.requests[0]["model"] == "gpt-6-luna"
            names = {tool["name"] for tool in model.requests[0]["tools"]}
            assert not names & {
                "run_terminal",
                "search_web",
                "start_browser_reader",
                "navigate",
                "evaluate",
            }
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_screenshots_enter_the_model_only_after_explicit_request(monkeypatch):
    async def scenario(url):
        def inspect_image(request):
            image_message = request["input"][-1]
            assert image_message["role"] == "user"
            assert image_message["content"][1]["type"] == "input_image"
            assert image_message["content"][1]["image_url"].startswith(
                "data:image/png;base64,"
            )
            return ResponsesResult(
                (), "Visual findings with uncertainty. Source: " + url
            )

        model = ScriptedModel([call("browser_screenshot"), inspect_image])
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        try:
            result = await finished(tools, url)
            assert result["state"] == "completed", result
            assert "Visual findings" in result["findings"]
            assert "input_image" not in json.dumps(model.requests[0]["input"])
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


@pytest.mark.parametrize("updated", [False, True])
def test_oversized_content_is_complete_and_optionally_searchable(monkeypatch, updated):
    async def scenario(url):
        def observation(request):
            observations = [
                json.loads(item["output"])
                for item in request["input"]
                if item.get("type") == "function_call_output"
                and '"snapshot_id"' in item["output"]
            ]
            return (
                observations[-1]
                if observations
                else json.loads(request["input"][1]["content"])
            )

        def click_large(request):
            initial = observation(request)
            ref = next(
                e["ref"] for e in initial["elements"] if e["label"] == "Large page"
            )
            return call("browser_click", ref=ref)

        def search(request):
            current = observation(request)
            assert current["overflow"] is True
            assert current["characters"] > 100000
            return call(
                "search_content",
                snapshot_id=current["snapshot_id"],
                query="Needle at the end.",
                offset=0,
            )

        def read_match(request):
            snapshot_id = json.loads(request["input"][-2]["arguments"])["snapshot_id"]
            matches = json.loads(request["input"][-1]["output"])["matches"]
            assert len(matches) == 1
            return call(
                "read_content",
                snapshot_id=snapshot_id,
                start=matches[0]["start"],
                end=matches[0]["end"],
            )

        def report(request):
            assert "Needle at the end." in request["input"][-1]["output"]
            return ResponsesResult(
                (), "Found the ending needle. Source: " + url + "large"
            )

        config = replace(
            WebReaderConfig(),
            context_tokens=7000,
            output_tokens=512,
            capture_chars=1,
            max_actions=1,
        )
        steps = ([click_large] if updated else []) + [search, read_match, report]
        tools = PlaywrightReaderTools(config, ScriptedModel(steps))
        try:
            result = await finished(tools, url if updated else url + "large")
            assert result["state"] == "completed", result
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


@pytest.mark.parametrize("limit", ["turn_limit", "context_limit"])
def test_limits_return_previously_reported_findings_without_history_eviction(
    monkeypatch, limit
):
    async def scenario(url):
        config = replace(
            WebReaderConfig(),
            max_attempts=1 if limit == "turn_limit" else 24,
            context_tokens=7000,
            output_tokens=512,
        )
        model = ScriptedModel(
            [
                call("report_findings", text="Useful partial findings. Source: " + url),
                call("report_findings", text="Large remaining output " * 10000),
            ]
        )
        tools = PlaywrightReaderTools(config, model)
        try:
            result = await finished(tools, url)
            assert result["state"] == "limit", result
            assert result["reason"] == limit
            assert "Useful partial findings" in result["findings"]
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_cancel_closes_active_reader_and_capacity_has_no_queue(monkeypatch):
    async def scenario(url):
        model = ControlledModel()
        tools = PlaywrightReaderTools(
            replace(WebReaderConfig(), max_active_readers=1), model
        )
        try:
            start = json.loads(
                await tools.execute(
                    "start_browser_reader", {"url": url, "prompt": "Investigate"}
                )
            )
            busy = json.loads(
                await tools.execute(
                    "start_browser_reader", {"url": url, "prompt": "Another"}
                )
            )
            assert busy["error"] == "reader_capacity"
            await asyncio.wait_for(model.started.wait(), 15)
            result = json.loads(
                await tools.execute(
                    "cancel_browser_reader", {"reader_id": start["reader_id"]}
                )
            )
            assert result["state"] == "cancelled"
            assert tools.readers[start["reader_id"]].browser.closed
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_earlier_main_deadline_wins(monkeypatch):
    async def scenario(url):
        model = ControlledModel()
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        tools.set_request_deadline(asyncio.get_running_loop().time() + 0.1)
        try:
            result = await finished(tools, url)
            assert result["state"] == "limit", result
            assert result["reason"] == "time_limit"
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_request_completion_cancels_unfinished_reader_without_late_findings(
    monkeypatch,
):
    async def scenario(url):
        model = ControlledModel()
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        start = json.loads(
            await tools.execute(
                "start_browser_reader", {"url": url, "prompt": "Investigate"}
            )
        )
        await asyncio.wait_for(model.started.wait(), 15)
        await tools.close_request()
        model.release.set()
        result = json.loads(
            await tools.execute(
                "browser_reader_status", {"reader_id": start["reader_id"]}
            )
        )
        assert result["state"] == "cancelled"
        assert not result["findings"]
        assert tools.readers[start["reader_id"]].browser.closed

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_unsupported_image_input_fails_clearly_without_model_fallback(monkeypatch):
    async def scenario(url):
        def reject_image(request):
            raise BadRequestError(
                "Configured model does not support image inputs",
                response=httpx.Response(
                    400,
                    request=httpx.Request(
                        "POST", "https://api.openai.com/v1/responses"
                    ),
                ),
                body=None,
            )

        model = ScriptedModel([call("browser_screenshot"), reject_image])
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        try:
            result = await finished(tools, url)
            assert result["state"] == "failed", result
            assert "screenshot_input_unsupported" in result["reason"]
            assert {r["model"] for r in model.requests} == {"gpt-6-luna"}
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_provider_context_limit_preserves_reported_findings(monkeypatch):
    async def scenario(url):
        def reject_context(request):
            raise BadRequestError(
                "Maximum context length exceeded",
                response=httpx.Response(
                    400,
                    request=httpx.Request(
                        "POST", "https://api.openai.com/v1/responses"
                    ),
                ),
                body={"code": "context_length_exceeded"},
            )

        model = ScriptedModel(
            [
                call("report_findings", text="Supported partial fact: " + url),
                reject_context,
            ]
        )
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        try:
            result = await finished(tools, url)
            assert result["state"] == "limit", result
            assert result["reason"] == "context_limit"
            assert "Supported partial fact" in result["findings"]
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_tool_call_commentary_does_not_replace_reported_findings_before_limit(
    monkeypatch,
):
    async def scenario(url):
        commentary = call("browser_inspect")
        commentary = ResponsesResult(commentary.output, "I will inspect more.")
        model = ScriptedModel(
            [call("report_findings", text="Supported partial fact: " + url), commentary]
        )
        tools = PlaywrightReaderTools(replace(WebReaderConfig(), max_attempts=2), model)
        try:
            result = await finished(tools, url)
            assert result["state"] == "limit", result
            assert "Supported partial fact" in result["findings"]
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_main_model_delegates_works_collects_and_closes_reader(monkeypatch):
    async def scenario(url):
        reader_model = ControlledModel()
        readers = PlaywrightReaderTools(WebReaderConfig(), reader_model)

        class OtherMainTool:
            definitions = (
                {
                    "type": "function",
                    "name": "other_work",
                    "description": "Other main work",
                    "parameters": {"type": "object", "properties": {}},
                },
            )

            async def execute(self, name, arguments):
                assert any(r.state == "running" for r in readers.readers.values())
                reader_model.release.set()
                return "42"

        def wait_for_reader(request):
            starts = [
                json.loads(item["output"])
                for item in request["input"]
                if item.get("type") == "function_call_output"
                and '"reader_id"' in item["output"]
            ]
            return call(
                "wait_browser_reader", reader_id=starts[0]["reader_id"], seconds=30
            )

        def synthesize(request):
            result = json.loads(request["input"][-1]["output"])
            assert result["state"] == "completed", result
            assert "small machine at 2" in result["findings"]
            assert any(item.get("output") == "42" for item in request["input"])
            return ResponsesResult((), "The small machine is 2. Source: " + url)

        main = ScriptedModel(
            [
                call("load_skill", name="web"),
                call(
                    "start_browser_reader",
                    url=url,
                    prompt="Check the small machine price",
                ),
                call("other_work"),
                wait_for_reader,
                synthesize,
            ]
        )
        tools = SkillTools(PreparedToolCollection(readers, OtherMainTool()))
        runner = DirectResponsesRunner(main, tools=tools, request_timeout_seconds=45)
        result = await runner.run(
            "Private main context: PRIVATE_SENTINEL. Research price.",
            model="gpt-6.1-sol",
            reasoning="low",
            system_prompt="Use the trial browser reader tools.",
        )
        assert isinstance(result, Completed)
        assert "small machine is 2" in result.reply
        assert "PRIVATE_SENTINEL" not in json.dumps(reader_model.requests)
        assert all(r.browser.closed for r in readers.readers.values())

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


@pytest.mark.skipif(
    not os.environ.get("JARVIS_TEST_READER_MODEL"),
    reason="Opt-in billed model screenshot capability check",
)
def test_configured_model_reads_canvas_only_screenshot(monkeypatch):
    # Explicit opt-in uses only the existing key; no secret is written or logged.
    from jarvis_personal_runtime.config import _parse_env

    async def scenario(url):
        values = _parse_env(Path(".env")) if Path(".env").exists() else {}
        key = (
            os.environ.get("OPENAI_API_KEY")
            or values.get("OPENAI_API_KEY")
            or values.get("JARVIS_OPENAI_API_KEY")
        )
        assert key, "An existing OPENAI_API_KEY is required"
        adapter = OpenAIRawResponsesAdapter.from_api_key(key)
        tools = PlaywrightReaderTools(WebReaderConfig(), adapter.without_retries())
        try:
            result = await finished(
                tools,
                url + "visual",
                "Request a screenshot. Read the large code drawn on the canvas. Return that exact code and the source URL; do not guess if you cannot see it.",
            )
            assert result["state"] == "completed", result
            assert "ORCHID 7391" in result["findings"], result
            assert url + "visual" in result["findings"], result
            print(
                json.dumps(
                    {
                        "model": "gpt-6-luna",
                        "state": result["state"],
                        "model_turns": result["model_turns"],
                        "canvas_code_read": True,
                        "findings": result["findings"],
                    }
                )
            )
        finally:
            await tools.close_request()
            await adapter.close()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))
