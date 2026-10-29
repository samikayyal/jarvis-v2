from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from jarvis_personal_runtime.config import WebDepthLimits, WebSearchConfig
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


def test_search_then_page_batch_shares_request_allowances(tmp_path):
    async def scenario():
        browser = Browser()
        tool = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=lambda *args: browser,
        )
        search = json.loads(
            await tool.execute(
                "search_web", {"query": "current value", "depth": "quick"}
            )
        )
        assert search["outcome"] == "ok"
        assert search["results"][0]["evidence_type"] == "search_result"
        assert search["remaining"]["queries"] == 0
        assert search["remaining"]["pages"] == 2
        again = json.loads(
            await tool.execute("search_web", {"query": "again", "depth": "quick"})
        )
        assert again["outcome"] == "limit_reached"
        pages = json.loads(
            await tool.execute(
                "read_web_pages",
                {
                    "urls": [
                        "https://example.com/one",
                        "https://example.com/two",
                        "https://example.com/three",
                    ],
                    "depth": "quick",
                },
            )
        )
        assert pages["outcome"] == "partial"
        assert pages["remaining"]["queries"] == pages["remaining"]["pages"] == 0
        assert pages["pages"][0]["evidence_type"] == "page"
        assert pages["pages"][2]["outcome"] == "unattempted"
        await tool.close_request()

    asyncio.run(scenario())


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
        answer = tool.finish_answer("Stable background.")
        if outcome == "no_results":
            assert "No useful search results were found." in answer
            assert "unavailable" not in answer
        else:
            assert "Web access is unavailable" in answer
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


@pytest.mark.parametrize("first_outcome", ["ok", "extraction_failed"])
def test_upgrade_carries_attempts_and_evidence_without_downgrade(
    tmp_path, first_outcome
):
    class ResearchBrowser(Browser):
        queries = 0

        async def search(self, query, limit):
            self.queries += 1
            if self.queries == 1 and first_outcome != "ok":
                return {"outcome": first_outcome}
            return await super().search(query, limit)

    async def scenario():
        browser = ResearchBrowser()
        tool = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=lambda *args: browser,
        )
        first = json.loads(
            await tool.execute("search_web", {"query": "first", "depth": "quick"})
        )
        assert first["outcome"] == first_outcome
        assert first["remaining"]["queries"] == 0
        direct = json.loads(
            await tool.execute(
                "read_web_pages", {"urls": ["https://example.com/a"], "depth": "quick"}
            )
        )
        assert direct["remaining"]["pages"] == 1
        upgraded = json.loads(
            await tool.execute("search_web", {"query": "compare", "depth": "moderate"})
        )
        assert upgraded["outcome"] == "ok"
        assert upgraded["remaining"]["queries"] == 1
        assert upgraded["remaining"]["pages"] == 4
        assert (
            upgraded["remaining"]["evidence_chars"]
            < direct["remaining"]["evidence_chars"]
        )
        last = json.loads(
            await tool.execute("search_web", {"query": "last", "depth": "quick"})
        )
        assert last["outcome"] == "ok"
        assert last["remaining"]["queries"] == 0
        capped = json.loads(
            await tool.execute("search_web", {"query": "over", "depth": "moderate"})
        )
        assert capped["outcome"] == "limit_reached"
        assert browser.queries == 3
        assert capped["remaining"] == last["remaining"]
        await tool.close_request()

    asyncio.run(scenario())


@pytest.mark.parametrize("first_depth", ["quick", "moderate"])
def test_page_led_research_uses_configured_limits_and_resets_next_request(
    tmp_path, first_depth
):
    async def scenario():
        browser = Browser()
        tool = WebTools(
            WebSearchConfig(
                enabled=True, quick=WebDepthLimits(1, 1), moderate=WebDepthLimits(2, 3)
            ),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=lambda *args: browser,
        )
        await tool.execute(
            "read_web_pages",
            {"urls": ["https://example.com/repeated"], "depth": first_depth},
        )
        upgraded = json.loads(
            await tool.execute(
                "read_web_pages",
                {"urls": ["https://example.com/repeated"] * 3, "depth": "moderate"},
            )
        )
        assert upgraded["outcome"] == "partial"
        assert upgraded["remaining"] == {
            "queries": 2,
            "pages": 0,
            "evidence_chars": 47973,
        }
        assert len(browser.urls) == 3
        assert upgraded["pages"][-1]["outcome"] == "unattempted"
        await tool.close_request()
        tool.request_started()
        fresh = json.loads(
            await tool.execute(
                "read_web_pages",
                {"urls": ["https://example.com/new"] * 2, "depth": "quick"},
            )
        )
        assert fresh["remaining"] == {"queries": 1, "pages": 0, "evidence_chars": 47991}
        assert fresh["pages"][-1]["outcome"] == "unattempted"
        await tool.close_request()

    asyncio.run(scenario())


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


def test_search_preserves_prior_evidence_when_later_blocked(tmp_path):
    class LaterBlocked(Browser):
        async def search(self, query, limit):
            if query == "second":
                return {"outcome": "blocked", "results": []}
            return await super().search(query, limit)

    async def scenario():
        base = WebSearchConfig(enabled=True)
        config = replace(
            base,
            quick=replace(base.quick, max_queries=3),
            max_chars_per_page=30,
            max_total_evidence_chars=30,
        )
        tool = WebTools(
            config,
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=LaterBlocked,
        )
        first = json.loads(
            await tool.execute("search_web", {"query": "first", "depth": "quick"})
        )
        assert first["remaining"]["evidence_chars"] == 0
        assert first["results"][0]["snippet"] == "Published eviden"
        cap = json.loads(
            await tool.execute("search_web", {"query": "second", "depth": "quick"})
        )
        assert cap["outcome"] == "limit_reached"
        assert "text allowance" in tool.finish_answer("Supported.")
        assert "unavailable" not in tool.finish_answer("Supported.")
        await tool.close_request()
        tool = WebTools(
            replace(base, quick=replace(base.quick, max_queries=3)),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=LaterBlocked,
        )
        await tool.execute("search_web", {"query": "first", "depth": "quick"})
        await tool.execute("search_web", {"query": "second", "depth": "quick"})
        assert "coverage is incomplete" in tool.finish_answer("Supported [1].")
        assert "existing knowledge" not in tool.finish_answer("Supported [1].")

    asyncio.run(scenario())


def test_page_allowance_is_shared_across_calls_and_approval_continuation(tmp_path):
    async def scenario():
        browsers = []

        def factory(*args, **kwargs):
            browser = Browser()
            browsers.append(browser)
            return browser

        tool = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=65536,
            browser_factory=factory,
        )
        tool.request_started()
        first = json.loads(
            await tool.execute(
                "read_web_pages",
                {"urls": ["https://example.com/one"], "depth": "quick"},
            )
        )
        assert first["remaining"]["pages"] == 1
        assert first["remaining"]["queries"] == 1
        await tool.close_request()
        assert browsers[0].closed
        second = json.loads(
            await tool.execute(
                "read_web_pages",
                {
                    "urls": ["https://example.com/two", "https://example.com/three"],
                    "depth": "quick",
                },
            )
        )
        assert second["outcome"] == "partial"
        assert second["pages"][0]["text"] == "Evidence."
        assert second["pages"][1]["outcome"] == "unattempted"
        assert second["remaining"]["pages"] == 0
        assert len(browsers) == 2
        assert browsers[1].urls == ["https://example.com/two"]
        await tool.close_request()
        tool.request_started()
        third = json.loads(
            await tool.execute(
                "read_web_pages",
                {"urls": ["https://example.com/three"], "depth": "quick"},
            )
        )
        assert third["remaining"]["pages"] == 1
        await tool.close_request()

    asyncio.run(scenario())


def test_failures_consume_attempts_and_disclose_only_real_access_failures(tmp_path):
    class FailingBrowser(Browser):
        async def read(self, url):
            self.urls.append(url)
            if url.endswith("good"):
                return await super().read(url)
            return {"outcome": "blocked", "reason": "Private redirect"}

    async def scenario():
        tool = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=65536,
            browser_factory=FailingBrowser,
        )
        result = json.loads(
            await tool.execute(
                "read_web_pages",
                {"urls": ["https://example.com/bad"], "depth": "quick"},
            )
        )
        assert result["remaining"]["pages"] == 1
        assert "existing knowledge" in tool.finish_answer("An answer")
        result = json.loads(
            await tool.execute(
                "read_web_pages",
                {"urls": ["https://example.com/good"], "depth": "quick"},
            )
        )
        assert result["remaining"]["pages"] == 0
        answer = tool.finish_answer("A supported answer [1]")
        assert "coverage is incomplete" in answer
        assert "existing knowledge" not in answer
        await tool.close_request()
        tool.request_started()
        await tool.execute(
            "read_web_pages",
            {"urls": ["https://example.com/good"] * 3, "depth": "quick"},
        )
        assert "page allowance" in tool.finish_answer("Answer")
        assert "unavailable" not in tool.finish_answer("Answer")
        await tool.close_request()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://localhost/",
        "https://user:password@example.com/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://example.com/\nInjected",
    ],
)
def test_disallowed_batch_rejected_before_any_attempt(tmp_path, url):
    browser = Browser()
    tool = WebTools(
        WebSearchConfig(enabled=True),
        runtime_dir=tmp_path,
        max_output_chars=65536,
        browser_factory=lambda *args: browser,
    )
    result = json.loads(
        asyncio.run(
            tool.execute(
                "read_web_pages",
                {"urls": ["https://example.com/", url], "depth": "quick"},
            )
        )
    )
    assert result["outcome"] == "blocked"
    assert result["remaining"]["pages"] == 2
    assert browser.urls == []


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
        await tool.execute(
            "read_web_pages", {"urls": ["https://example.com"], "depth": "quick"}
        )
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


@pytest.mark.parametrize("capacity", [800, 1000])
def test_evidence_is_shortened_inside_valid_json_with_source_identity(
    tmp_path, capacity
):
    class LongBrowser(Browser):
        async def read(self, url):
            return {
                "outcome": "ok",
                "title": 'A "quoted" source',
                "url": url,
                "text": '\\"\nالعربية' * 1000,
            }

    config = replace(
        WebSearchConfig(enabled=True),
        max_chars_per_page=400,
        max_total_evidence_chars=600,
    )
    tool = WebTools(
        config,
        runtime_dir=tmp_path,
        max_output_chars=capacity,
        browser_factory=LongBrowser,
    )
    raw = asyncio.run(
        tool.execute(
            "read_web_pages",
            {
                "urls": ["https://example.com/a", "https://example.com/b"],
                "depth": "quick",
            },
        )
    )
    assert len(raw) <= capacity
    result = json.loads(raw)
    if capacity == 800:
        # Both sources' full extraction metadata requires more than 800 chars
        # even with empty text. Reject explicitly instead of dropping metadata.
        assert result["outcome"] == "limit_reached"
        assert result["stop_reason"] == "output_limit"
        assert tool._remaining()["evidence_chars"] == 600
        return
    assert [page["url"] for page in result["pages"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert all(page["title"] == 'A "quoted" source' for page in result["pages"])
    assert all(page["extraction_method"] == "body_prefix" for page in result["pages"])
    evidence = sum(len(page["text"]) for page in result["pages"])
    assert 0 < evidence <= 600
    assert result["remaining"]["evidence_chars"] == 600 - evidence


def test_unavailable_worker_is_not_restarted_even_after_suspension(tmp_path):
    class UnavailableBrowser(Browser):
        async def read(self, url):
            self.urls.append(url)
            return {"outcome": "unavailable"}

    async def scenario():
        browser = UnavailableBrowser()
        tool = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=65536,
            browser_factory=lambda *args: browser,
        )
        await tool.execute(
            "read_web_pages", {"urls": ["https://example.com/a"], "depth": "quick"}
        )
        await tool.close_request()
        result = json.loads(
            await tool.execute(
                "read_web_pages", {"urls": ["https://example.com/b"], "depth": "quick"}
            )
        )
        assert browser.urls == ["https://example.com/a"]
        assert result["pages"][0]["outcome"] == "unattempted"
        assert result["pages"][0]["reason"] == "worker_unavailable"
        assert result["remaining"]["pages"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("usable", [False, True])
def test_timeout_only_continues_with_a_usable_worker(tmp_path, usable):
    class TimeoutBrowser(Browser):
        async def read(self, url):
            self.urls.append(url)
            if len(self.urls) == 1:
                return {"outcome": "timeout", "usable": usable}
            return {"outcome": "ok", "title": "Second", "url": url, "text": "Evidence"}

    browser = TimeoutBrowser()
    tool = WebTools(
        WebSearchConfig(enabled=True),
        runtime_dir=tmp_path,
        max_output_chars=65536,
        browser_factory=lambda *args: browser,
    )
    result = json.loads(
        asyncio.run(
            tool.execute(
                "read_web_pages",
                {
                    "urls": ["https://example.com/slow", "https://example.com/next"],
                    "depth": "quick",
                },
            )
        )
    )
    assert len(browser.urls) == (2 if usable else 1)
    assert result["pages"][1]["outcome"] == ("ok" if usable else "unattempted")
    assert result["remaining"]["pages"] == (0 if usable else 1)


@pytest.mark.parametrize("limit", [2, 40, 200])
def test_tiny_runtime_output_limits_return_valid_json_without_navigation(
    tmp_path, limit
):
    browser = Browser()
    tool = WebTools(
        WebSearchConfig(enabled=True),
        runtime_dir=tmp_path,
        max_output_chars=limit,
        browser_factory=lambda *args: browser,
    )
    output = asyncio.run(
        tool.execute(
            "read_web_pages",
            {"urls": ["https://example.com/" + "a" * 100], "depth": "quick"},
        )
    )
    assert len(output) <= limit
    assert isinstance(json.loads(output), dict)
    assert browser.urls == []
