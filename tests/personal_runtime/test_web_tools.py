from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from jarvis_personal_runtime.config import WebSearchConfig
from jarvis_personal_runtime.web_tools import WebTools


class Browser:
    def __init__(self, *args, **kwargs):
        self.urls = []
        self.closed = False

    async def read(self, url):
        self.urls.append(url)
        return {"outcome": "ok", "title": "Source", "url": url, "text": "Evidence."}

    async def close(self):
        self.closed = True

    async def search(self, query, limit):
        return {
            "outcome": "ok",
            "results": [
                {
                    "title": "Primary source",
                    "url": "https://example.com/source",
                    "snippet": "Published evidence.",
                    "evidence_type": "search_result",
                }
            ],
        }


@pytest.mark.parametrize(
    "outcome", ["blocked", "unavailable", "extraction_failed", "no_results"]
)
def test_search_outcomes_and_attempts_survive_cleanup(tmp_path, outcome):
    class SearchBrowser(Browser):
        attempts = 0

        async def search(self, query, limit):
            self.attempts += 1
            return {"outcome": outcome, "results": []}

    async def scenario():
        browser = SearchBrowser()
        base = WebSearchConfig(enabled=True)
        config = replace(base, quick=replace(base.quick, max_queries=3))
        tool = WebTools(
            config,
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=lambda *args: browser,
        )
        first = json.loads(
            await tool.execute("search_web", {"query": "fresh fact", "depth": "quick"})
        )
        assert first["outcome"] == outcome
        assert first["remaining"]["queries"] == 2
        await tool.close_request()
        second = json.loads(
            await tool.execute("search_web", {"query": "another", "depth": "quick"})
        )
        assert second["outcome"] == outcome
        assert browser.attempts == (1 if outcome in {"blocked", "unavailable"} else 2)
        if outcome in {"extraction_failed", "no_results"}:
            await tool.execute("search_web", {"query": "last", "depth": "quick"})
            capped = json.loads(
                await tool.execute("search_web", {"query": "over", "depth": "quick"})
            )
            assert capped["outcome"] == "limit_reached"
            assert capped["remaining"]["queries"] == 0
            assert browser.attempts == 3
        await tool.close_request()
        tool.request_started()
        await tool.execute("search_web", {"query": "new request", "depth": "quick"})
        assert browser.attempts == (2 if outcome in {"blocked", "unavailable"} else 4)

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [2, 40, 200, 400, 1024])
def test_search_serialization_always_fits_and_preserves_source_identity(
    tmp_path, limit
):
    class LargeBrowser(Browser):
        async def search(self, query, limit):
            return {
                "outcome": "ok",
                "results": [
                    {
                        "title": "Source",
                        "url": "https://example.com/" + str(i),
                        "snippet": '\\"\n' * 4000,
                    }
                    for i in range(10)
                ],
            }

    async def scenario():
        tool = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=limit,
            browser_factory=LargeBrowser,
        )
        output = await tool.execute("search_web", {"query": "news", "depth": "quick"})
        result = json.loads(output)
        assert len(output) <= limit
        assert len(result.get("results", [])) <= 5
        for source in result.get("results", []):
            assert source["title"] == "Source"
            assert source["url"] in {f"https://example.com/{i}" for i in range(5)}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": " ", "depth": "quick"},
        {"query": "news", "depth": "deep"},
        {"query": ["a", "b"], "depth": "quick"},
        {"query": "a" * 2049, "depth": "quick"},
    ],
)
def test_invalid_search_does_not_consume_allowance(tmp_path, arguments):
    tool = WebTools(
        WebSearchConfig(enabled=True),
        runtime_dir=tmp_path,
        max_output_chars=4096,
        browser_factory=Browser,
    )
    result = json.loads(asyncio.run(tool.execute("search_web", arguments)))
    assert result["outcome"] == "invalid_input"
    assert result["remaining"]["queries"] == 1


def test_failed_search_json_fits_exact_small_output_boundary(tmp_path):
    class FailedBrowser(Browser):
        async def search(self, query, limit):
            return {"outcome": "extraction_failed", "results": []}

    tool = WebTools(
        WebSearchConfig(enabled=True),
        runtime_dir=tmp_path,
        max_output_chars=114,
        browser_factory=FailedBrowser,
    )
    output = asyncio.run(tool.execute("search_web", {"query": "q", "depth": "quick"}))
    assert len(output) <= 114
    assert json.loads(output)["outcome"] in {"limit_reached", "extraction_failed"}


@pytest.mark.parametrize("output_limit", [180, 4096])
def test_search_preserves_bounded_failure_reason(tmp_path, output_limit):
    class BlockedBrowser(Browser):
        async def search(self, query, limit):
            return {
                "outcome": "blocked",
                "results": [],
                "reason": "Google requires JavaScript to search",
            }

    tool = WebTools(
        WebSearchConfig(enabled=True),
        runtime_dir=tmp_path,
        max_output_chars=output_limit,
        browser_factory=BlockedBrowser,
    )
    output = asyncio.run(tool.execute("search_web", {"query": "q", "depth": "quick"}))
    result = json.loads(output)
    assert len(output) <= output_limit
    assert result["outcome"] == "blocked"
    if output_limit == 4096:
        assert result["reason"] == "Google requires JavaScript to search"


def test_cleanup_survives_a_second_cancellation(tmp_path):
    async def scenario():
        started = asyncio.Event()
        released = asyncio.Event()

        class SlowClose(Browser):
            async def close(self):
                started.set()
                await released.wait()
                self.closed = True

        browser = SlowClose()
        tool = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=65536,
            browser_factory=lambda *args: browser,
        )
        await tool.execute("search_web", {"query": "current fact", "depth": "quick"})
        cleanup = asyncio.create_task(tool.close_request())
        await started.wait()
        cleanup.cancel()
        await asyncio.sleep(0)
        assert not browser.closed
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert browser.closed

    asyncio.run(scenario())


def test_search_upgrade_preserves_usage_and_resets_only_for_a_new_request(tmp_path):
    async def scenario():
        tool = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=Browser,
        )

        async def search(depth):
            return json.loads(
                await tool.execute(
                    "search_web", {"query": "published value", "depth": depth}
                )
            )

        assert (await search("quick"))["remaining"]["queries"] == 0
        await tool.close_request()  # Approval suspension preserves consumed usage.
        assert (await search("moderate"))["remaining"]["queries"] == 1
        assert (await search("quick"))["remaining"]["queries"] == 0
        assert (await search("moderate"))["outcome"] == "limit_reached"
        await tool.close_request()
        tool.request_started()
        assert (await search("quick"))["outcome"] == "ok"
        await tool.close_request()

    asyncio.run(scenario())
