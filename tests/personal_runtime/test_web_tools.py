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
        assert tool.finish_answer("Answer") == "Answer"
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


def test_evidence_is_shortened_inside_valid_json_with_source_identity(tmp_path):
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
        config, runtime_dir=tmp_path, max_output_chars=800, browser_factory=LongBrowser
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
    assert len(raw) <= 800
    result = json.loads(raw)
    assert [page["url"] for page in result["pages"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert all(page["title"] == 'A "quoted" source' for page in result["pages"])
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
