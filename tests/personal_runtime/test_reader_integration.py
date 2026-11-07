"""Normal runtime assembly with a scripted provider and real isolated browsers."""

import asyncio
import copy
import json

from jarvis_personal_runtime.config import (
    RuntimeConfig,
    WebReaderConfig,
    WebSearchConfig,
)
from jarvis_personal_runtime.responses import (
    OpenAIRawResponsesAdapter,
    ResponsesResult,
    build_direct_responses_runner,
)

from .test_playwright_browser import public_fixture


def test_default_runner_delegates_does_other_work_synthesizes_and_releases(
    monkeypatch, tmp_path
):
    async def scenario(url):
        (tmp_path / "context.md").write_text("Existing setup: local hosting.")

        class Provider:
            def __init__(self):
                self.requests = []
                self.step = 0
                self.ids = []
                self.reader_arrived = asyncio.Event()
                self.release = asyncio.Event()

            def without_retries(self):
                return self

            async def create(self, request, *, timeout):
                self.requests.append(copy.deepcopy(request))
                if request["model"] == "gpt-6-luna":
                    self.reader_arrived.set()
                    await self.release.wait()
                    return ResponsesResult(
                        (), "Compute costs 2; storage is unverified. " + url
                    )
                self.step += 1
                names = {t["name"] for t in request["tools"]}
                if self.step == 1:
                    assert "start_browser_reader" not in names
                    return self.call("load_skill", name="web")
                assert {
                    "start_browser_reader",
                    "browser_reader_status",
                    "wait_browser_reader",
                    "follow_up_browser_reader",
                    "cancel_browser_reader",
                    "release_browser_reader",
                } <= names
                assert (
                    not {"read_web_page", "ask_page_reader", "read_web_pages"} & names
                )
                if self.step in {2, 3}:
                    if self.step == 3:
                        self.ids.append(
                            json.loads(request["input"][-1]["output"])["reader_id"]
                        )
                    return self.call(
                        "start_browser_reader",
                        url=url,
                        prompt="Compare compute prices.",
                    )
                if self.step == 4:
                    self.ids.append(
                        json.loads(request["input"][-1]["output"])["reader_id"]
                    )
                    await asyncio.wait_for(self.reader_arrived.wait(), 60)
                    return self.call("load_skill", name="obsidian")
                if self.step == 5:
                    assert not self.release.is_set()
                    assert runner.progress() == "Web research: 2 running."
                    return self.call("read_vault", mode="read", value="context.md")
                if self.step == 6:
                    assert "local hosting" in request["input"][-1]["output"]
                    self.release.set()
                    return self.call(
                        "wait_browser_reader", reader_id=self.ids[0], seconds=30
                    )
                if self.step == 7:
                    assert "storage is unverified" in request["input"][-1]["output"]
                    return self.call("release_browser_reader", reader_id=self.ids[1])
                if self.step == 9:
                    assert runner.progress() is None
                    return self.call("browser_reader_status", reader_id=self.ids[0])
                if self.step == 10:
                    state = json.loads(request["input"][-1]["output"])
                    assert (
                        state["state"] == "completed"
                        and state["browser_state"] == "closed"
                    )
                    return self.call("browser_reader_status", reader_id=self.ids[1])
                if self.step == 11:
                    assert (
                        json.loads(request["input"][-1]["output"])["error"]
                        == "expired_reader_id"
                    )
                    return ResponsesResult(
                        (), "Retained findings available; browsers closed."
                    )
                return ResponsesResult(
                    (), "Compute: 2. Storage remains unverified. [1] " + url
                )

            def call(self, tool_name, **args):
                return ResponsesResult(
                    (
                        {
                            "type": "function_call",
                            "name": tool_name,
                            "call_id": str(self.step),
                            "arguments": json.dumps(args),
                        },
                    ),
                    "",
                )

        provider = Provider()
        monkeypatch.setattr(
            OpenAIRawResponsesAdapter, "from_api_key", lambda *a, **k: provider
        )
        config = RuntimeConfig(
            root=tmp_path,
            vault_path=tmp_path,
            trace_path=tmp_path / "trace.jsonl",
            web_search=WebSearchConfig(enabled=True),
            web_reader=WebReaderConfig(),
            max_tool_rounds=20,
        )
        runner = build_direct_responses_runner("unused-test-credential", config)
        result = await runner.run(
            "Compare sources; keep the reply short.",
            model="gpt-6-sol",
            reasoning="low",
            system_prompt="Help concisely.",
        )
        assert result.reply == "Compute: 2. Storage remains unverified. [1] " + url
        # Findings survive completion, but request-owned interactive state does not.
        retained = await runner.run(
            "Check retained readers.",
            model="gpt-6-sol",
            reasoning="low",
            system_prompt="Help concisely.",
        )
        assert retained.reply == "Retained findings available; browsers closed."
        events = [
            json.loads(line) for line in config.trace_path.read_text().splitlines()
        ]
        main_calls = [e["payload"] for e in events if e["event"] == "responses_request"]
        assert len({e["run_id"] for e in main_calls}) == 2
        assert [e["model_call"] for e in main_calls[:8]] == list(range(1, 9))
        reader_calls = [
            e["payload"] for e in events if e["event"] == "playwright_reader_request"
        ]
        assert reader_calls and all(
            e["assignment_id"] and e["model_call"] == 1 for e in reader_calls
        )
        outputs = [
            e["payload"] for e in events if e["event"] == "playwright_reader_output"
        ]
        assert outputs and all(e["elapsed_seconds"] >= 0 for e in outputs)

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_missing_reader_configuration_fails_explicitly_without_fallback(
    monkeypatch, tmp_path
):
    class Provider:
        def without_retries(self):
            return self

        async def create(self, request, *, timeout):
            outputs = [
                i for i in request["input"] if i.get("type") == "function_call_output"
            ]
            if not outputs:
                name, arguments = "load_skill", {"name": "web"}
            elif len(outputs) == 1:
                name, arguments = (
                    "start_browser_reader",
                    {"url": "https://example.com", "prompt": "Read the source."},
                )
            else:
                result = json.loads(outputs[-1]["output"])
                assert result["error"] == "reader_configuration_error"
                return ResponsesResult((), "Reader configuration needs repair.")
            return ResponsesResult(
                (
                    {
                        "type": "function_call",
                        "name": name,
                        "call_id": str(len(outputs)),
                        "arguments": json.dumps(arguments),
                    },
                ),
                "",
            )

    provider = Provider()
    monkeypatch.setattr(
        OpenAIRawResponsesAdapter, "from_api_key", lambda *a, **k: provider
    )
    config = RuntimeConfig(
        root=tmp_path,
        trace_path=tmp_path / "trace.jsonl",
        web_search=WebSearchConfig(enabled=True),
    )
    runner = build_direct_responses_runner("unused-test-credential", config)
    result = asyncio.run(
        runner.run(
            "Read the source", model="gpt-6-sol", reasoning="low", system_prompt="Help."
        )
    )
    assert result.reply == "Reader configuration needs repair."
