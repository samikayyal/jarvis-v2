from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from jarvis_personal_runtime.config import WebSearchConfig
from jarvis_personal_runtime.web_evidence import is_access_challenge
from jarvis_personal_runtime.web_tools import WebTools


@pytest.mark.parametrize(
    ("body", "title", "challenge", "blocked"),
    [
        ("Verify you are human", "", False, True),
        ("Please wait", "Access denied", False, True),
        ("Cloudflare\nChecking your browser...\nPlease wait", "", False, True),
        ("Public article about access denied errors.", "Documentation", False, False),
        ("How to resolve access denied\nUse these steps", "Guide", False, False),
        ("Verify you are human\n" + "Article prose. " * 200, "Guide", False, False),
        ("Public text", "", True, True),
    ],
)
def test_challenge_detection_distinguishes_interstitials_from_prose(
    body, title, challenge, blocked
):
    assert is_access_challenge(body, title, challenge=challenge) is blocked


class Pages:
    def __init__(self, *, size=8000, outcome="ok", rendered=False):
        self.size = size
        self.outcome = outcome
        self.rendered = rendered
        self.calls = []
        self.closed = False

    async def read(self, url, *, wait_for_text=None):
        self.calls.append((url, wait_for_text))
        return {
            "outcome": self.outcome,
            "url": url,
            "title": "Pricing",
            "text": "E" * self.size,
            "source_chars": self.size + 100,
            "truncated": True,
            "section_found": False,
            "render_mode": "rendered" if self.rendered else "static",
            "render_wait": "matched" if self.rendered else "not_requested",
        }

    async def search(self, query, limit):
        return {
            "outcome": "blocked",
            "reason": "Google blocked automated search traffic",
            "results": [],
        }

    async def close(self):
        self.closed = True


def make_tool(tmp_path, static, rendered=None, *, total=48000, output=65536):
    return WebTools(
        replace(WebSearchConfig(enabled=True), max_total_evidence_chars=total),
        runtime_dir=tmp_path,
        max_output_chars=output,
        browser_factory=lambda *a: static,
        rendered_browser_factory=lambda *a: rendered,
    )


async def read(tool, urls, *, mode="static", wait=None, depth="moderate"):
    return json.loads(
        await tool.execute(
            "read_web_pages",
            {
                "urls": urls,
                "depth": depth,
                "render_mode": mode,
                "wait_for_text": wait,
            },
        )
    )


def test_five_full_pages_fit_new_default_and_preserve_extraction_facts(tmp_path):
    async def scenario():
        browser = Pages()
        tool = make_tool(tmp_path, browser)
        assert WebSearchConfig().max_total_evidence_chars == 48000
        result = await read(tool, [f"https://example.com/{i}" for i in range(5)])
        assert result["outcome"] == "ok"
        assert result["remaining"] == {"queries": 3, "pages": 0, "evidence_chars": 8000}
        assert all(
            p["truncated"] and p["section_found"] is False for p in result["pages"]
        )
        assert all(p["source_chars"] == 8100 for p in result["pages"])
        assert all(p["stop_reason"] == "page_text_limit" for p in result["pages"])
        assert tool.finish_answer("Supported") == "Supported"
        await tool.close_request()

    asyncio.run(scenario())


def test_trace_scenario_reports_google_block_and_exhausted_text_separately(tmp_path):
    async def scenario():
        browser = Pages()
        tool = make_tool(tmp_path, browser, total=32000)
        search = json.loads(
            await tool.execute(
                "search_web", {"query": "VM pricing", "depth": "moderate"}
            )
        )
        assert search["reason"] == "Google blocked automated search traffic"
        result = await read(tool, [f"https://example.com/{i}" for i in range(5)])
        assert result["stop_reason"] == "evidence_limit"
        assert result["pages"][4]["reason"] == "evidence_limit"
        assert result["remaining"]["pages"] == 1
        assert result["remaining"]["evidence_chars"] == 0
        for depth in ("quick", "moderate"):
            later = await read(tool, ["https://example.com/later#prices"], depth=depth)
            assert later["stop_reason"] == "evidence_limit"
        assert len(browser.calls) == 4
        answer = tool.finish_answer("Prices remain unverified. [1]")
        assert "Google search was blocked." in answer
        assert "Read 4 page excerpts." in answer
        assert "text allowance" in answer
        assert "Prices remain unverified. [1]" in answer
        assert "existing knowledge" not in answer
        await tool.close_request()

    asyncio.run(scenario())


def test_rendered_read_requires_static_evidence_counts_and_closes_both_workers(
    tmp_path,
):
    async def scenario():
        static, dynamic = Pages(size=100), Pages(size=200, rendered=True)
        tool = make_tool(tmp_path, static, dynamic)
        url = "https://example.com/prices"
        rejected = await read(tool, [url], mode="rendered", wait="E2 hourly")
        assert rejected["outcome"] == "invalid_input"
        assert not dynamic.calls
        await read(tool, [url])
        result = await read(tool, [url], mode="rendered", wait="E2 hourly")
        assert result["pages"][0]["render_wait"] == "matched"
        assert result["pages"][0]["render_mode"] == "rendered"
        assert result["remaining"]["pages"] == 3
        assert result["remaining"]["evidence_chars"] == 47700
        assert dynamic.calls == [(url, "E2 hourly")]
        await tool.close_request()
        assert static.closed and dynamic.closed
        # Suspension preserves accounting and render eligibility.
        await read(tool, [url], mode="rendered", wait="E2 hourly")
        assert tool._remaining()["pages"] == 2
        await tool.close_request()
        tool.request_started()
        assert (await read(tool, [url], mode="rendered", wait="E2 hourly"))[
            "outcome"
        ] == "invalid_input"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "outcome", ["blocked", "unsupported", "timeout", "unavailable"]
)
def test_rendering_never_retries_failed_access(tmp_path, outcome):
    async def scenario():
        static, dynamic = Pages(outcome=outcome), Pages(rendered=True)
        tool = make_tool(tmp_path, static, dynamic)
        url = "https://example.com/prices"
        await read(tool, [url])
        result = await read(tool, [url], mode="rendered", wait="price")
        assert result["outcome"] == "invalid_input"
        assert not dynamic.calls
        await tool.close_request()

    asyncio.run(scenario())


@pytest.mark.parametrize("wait", [None, "", " " * 3, "x" * 201, 4])
def test_invalid_render_wait_cannot_start_browser(tmp_path, wait):
    browser = Pages()
    result = asyncio.run(
        read(
            make_tool(tmp_path, browser),
            ["https://example.com"],
            mode="rendered",
            wait=wait,
        )
    )
    assert result["outcome"] == "invalid_input"
    assert not browser.calls


def test_repeated_blocked_page_is_not_reported_as_budget_exhaustion(tmp_path):
    async def scenario():
        browser = Pages(outcome="blocked")
        tool = make_tool(tmp_path, browser)
        await read(tool, ["https://example.com/page#one"])
        result = await read(tool, ["https://example.com/page#two"])
        assert result["outcome"] == "blocked"
        assert result["pages"][0]["reason"] == "previous_access_block"
        assert result["remaining"]["pages"] == 4
        assert len(browser.calls) == 1
        await tool.close_request()

    asyncio.run(scenario())


def test_output_clipping_preserves_metadata_and_charges_only_returned_text(tmp_path):
    async def scenario():
        browser = Pages(size=8000)
        tool = make_tool(tmp_path, browser, output=700)
        result = await read(tool, ["https://example.com/prices"])
        page = result["pages"][0]
        assert page["truncated"] is True
        assert page["stop_reason"] == "output_limit"
        assert page["source_chars"] == 8100
        assert page["url"] == "https://example.com/prices"
        assert 0 < len(page["text"]) < 8000
        assert result["remaining"]["evidence_chars"] == 48000 - len(page["text"])
        assert "output limit" in tool.finish_answer("Supported excerpt")
        await tool.close_request()

    asyncio.run(scenario())


def test_render_wait_timeout_is_not_claimed_as_verified_dynamic_content(tmp_path):
    class NotReady(Pages):
        async def read(self, url, **kwargs):
            result = await super().read(url, **kwargs)
            result.update(render_wait="timeout", stop_reason="render_wait_timeout")
            return result

    async def scenario():
        tool = make_tool(tmp_path, Pages(size=100), NotReady(size=100, rendered=True))
        url = "https://example.com/prices"
        await read(tool, [url])
        result = await read(tool, [url], mode="rendered", wait="hourly price")
        assert result["pages"][0]["outcome"] == "ok"
        assert result["pages"][0]["stop_reason"] == "render_wait_timeout"
        assert "did not appear" in tool.finish_answer("Only the introduction was read.")
        await tool.close_request()

    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", range(200, 2500, 113))
def test_final_limit_metadata_never_charges_omitted_evidence(tmp_path, capacity):
    async def scenario():
        tool = make_tool(tmp_path, Pages(), output=capacity)
        raw = await tool.execute(
            "read_web_pages",
            {
                "urls": ["https://example.com/a"] * 5,
                "depth": "moderate",
            },
        )
        result = json.loads(raw)
        returned = sum(len(p["text"]) for p in result.get("pages", []))
        assert len(raw) <= capacity
        assert tool._remaining()["evidence_chars"] == 48000 - returned
        if "remaining" in result:
            assert result["remaining"]["evidence_chars"] == 48000 - returned
        await tool.close_request()

    asyncio.run(scenario())


def test_oversized_batch_is_rejected_before_browser_work(tmp_path):
    browser = Pages()
    tool = make_tool(tmp_path, browser)
    result = asyncio.run(read(tool, ["https://example.com/a"] * 6))
    assert result["outcome"] == "invalid_input"
    assert not browser.calls
    assert tool._remaining()["pages"] == 2


def test_output_fitting_retains_complete_small_source_metadata(tmp_path):
    tool = make_tool(tmp_path, Pages(), output=600)
    small = {
        "text": "Complete",
        "outcome": "ok",
        "url": "https://example.com/a",
        "truncated": False,
        "more_content_available": False,
    }
    large = {"text": '"\\\n' * 2000, "outcome": "ok", "url": "https://example.com/b"}
    original = dict(small)
    result = {"pages": [small, large]}
    assert tool._fit_text(result, result["pages"])
    assert small == original
    assert large["truncated"] is True
    assert large["stop_reason"] == "output_limit"
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 600


def test_successful_reread_clears_resolved_page_failure_notice(tmp_path):
    async def scenario():
        browser = Pages(outcome="extraction_failed")
        tool = make_tool(tmp_path, browser)
        url = "https://example.com/a"
        await read(tool, [url])
        assert "could not verify" in tool.finish_answer("No evidence")
        browser.outcome = "ok"
        await read(tool, [url])
        assert tool.finish_answer("Verified excerpt") == "Verified excerpt"
        await tool.close_request()

    asyncio.run(scenario())
