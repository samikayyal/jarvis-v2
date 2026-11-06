"""Behavioral checks for retrieval, mandatory fallback and honest coverage."""

import asyncio
import json
from dataclasses import replace

import pytest

from jarvis_personal_runtime.page_document import split_document
from jarvis_personal_runtime.page_reader import Reader, ReaderLimit
from jarvis_personal_runtime.responses import ResponsesResult

from .test_page_reader import URL, Browser, Responses, answer, ask, call, create, setup


def unresolved(*, display_checked=True, **extra):
    return ResponsesResult(
        (),
        json.dumps(
            {
                "answer": "Not found yet",
                "answered": False,
                "display_checked": display_checked,
                "evidence": [],
                "gaps": ["Billing minimum remains unverified"],
                "links": [],
                **extra,
            }
        ),
    )


def long_page():
    return (
        "# Overview\n\n"
        + "Other specifications. " * 180
        + "\n\n# Miscellaneous\n\nAccounts accrue a charge of at least sixty seconds."
    )


def test_search_ids_are_only_leads_and_skipped_match_is_inspected(tmp_path):
    text = long_page()
    chunks = split_document(text, 128)
    target = chunks[-1]

    async def scenario():
        browser = Browser({"outcome": "ok", "text": text})
        model = Responses(
            call("search_page", terms=["SIXTY   SECONDS", "sixty"]),
            unresolved(),
            answer(chunk=target["id"], quote="sixty seconds"),
        )
        tools = setup(tmp_path, model, browser, chunk_tokens=128)
        result = await create(tools)
        search_output = json.loads(model.requests[1]["input"][-1]["output"])
        assert search_output == [target["id"]]
        assert "sixty seconds" not in json.dumps(model.requests[1]["input"])
        assert result["coverage"]["inspected"] == [target["id"]]
        assert result["answered"] and len(browser.calls) == 1
        assert "Required fallback inspection" in json.dumps(model.requests[2])

    asyncio.run(scenario())


def test_two_search_misses_then_display_review_then_sweep_finds_paraphrase(tmp_path):
    text = long_page()
    chunks = split_document(text, 128)

    async def scenario():
        model = Responses(
            unresolved(),
            call("search_page", terms=["minimum billing"]),
            unresolved(),
            call("search_page", terms=["billing duration"]),
            unresolved(display_checked=False),
            unresolved(),
            *[unresolved() for _ in chunks[:-1]],
            answer(chunk=chunks[-1]["id"], quote="sixty seconds"),
        )
        browser = Browser({"outcome": "ok", "text": text})
        tools = setup(tmp_path, model, browser, chunk_tokens=128)
        result = await create(tools)
        assert result["answered"]
        assert result["coverage"]["inspection"] == "all_captured"
        assert model.requests[1]["tool_choice"]["name"] == "search_page"
        assert model.requests[3]["tool_choice"]["name"] == "search_page"
        assert [t["name"] for t in model.requests[5]["tools"]] == ["display_page"]
        assert len(browser.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("terms", [[], [" "], ["x"] * 21, ["x" * 201], [1], "billing"])
def test_search_rejects_invalid_input_without_marking_inspected(tmp_path, terms):
    tools = setup(tmp_path, Responses(), Browser())
    reader = Reader(URL, chunks=[{"id": "1", "text": "billing"}])
    with pytest.raises(ValueError):
        tools._search(reader, terms)
    assert not reader.inspected


def test_search_normalizes_case_whitespace_and_deduplicates_in_capture_order(tmp_path):
    tools = setup(tmp_path, Responses(), Browser())
    reader = Reader(
        URL,
        chunks=[
            {"id": "2", "text": "MINIMUM\n\t BILLING"},
            {"id": "9", "text": "Other text"},
            {"id": "11", "text": "minimum billing and fees"},
        ],
    )
    assert tools._search(reader, ["minimum billing", "fees", "BILLING"]) == ["2", "11"]
    assert tools._search(reader, ["minimum charge"]) == []
    assert not reader.inspected


@pytest.mark.parametrize(
    "limit, expected", [("actions", "action_limit"), ("attempts", "attempt_limit")]
)
def test_fallback_limit_preserves_partial_coverage_and_final_notice(
    tmp_path, limit, expected
):
    async def scenario():
        model = Responses(
            unresolved(), call("search_page", terms=["sixty"]), unresolved()
        )
        tools = setup(
            tmp_path,
            model,
            Browser({"outcome": "ok", "text": long_page()}),
            chunk_tokens=128,
            **{"max_" + limit: 1},
        )
        result = await create(tools)
        assert result["outcome"] == expected
        assert not result["answered"]
        assert result["coverage"]["inspection"] == "partial"
        assert URL in tools.finish_answer("Another source supplies the answer.")
        assert expected.replace("_", " ") in tools.finish_answer("Done")

    asyncio.run(scenario())


def test_followup_never_inherits_answered_and_unrelated_question_cannot_clear_notice(
    tmp_path,
):
    async def scenario():
        model = Responses(
            answer(quote="hourly price"),
            unresolved(),
            answer(quote="hourly price"),
            answer(quote="hourly price"),
        )
        tools = setup(
            tmp_path, model, Browser({"outcome": "ok", "text": "hourly price"})
        )
        first = await create(tools, "Price?")
        tools.request_started()
        missing = await ask(tools, first["reader_id"], "Minimum billing?")
        assert not missing["answered"] and not missing["evidence"]
        assert missing["coverage"]["inspection"] == "all_captured"
        assert "Source check unresolved" in tools.finish_answer("Done")
        await ask(tools, first["reader_id"], "Price?")
        assert "Source check unresolved" in tools.finish_answer("Done")
        await ask(tools, first["reader_id"], "Minimum billing?")
        assert tools.finish_answer("Done") == "Done"
        # Quote occurrence is enforced; whether this quote really answers the new
        # question deliberately remains model judgment, represented by the fixture.

    asyncio.run(scenario())


def test_repeated_read_does_not_duplicate_body(tmp_path):
    async def scenario():
        text = "Unique captured body"
        model = Responses(call("read_page_chunk", chunk_id="1"), answer(quote=text))
        tools = setup(tmp_path, model, Browser({"outcome": "ok", "text": text}))
        result = await create(tools)
        assert result["answered"]
        assert json.dumps(model.requests[-1]["input"]).count(text) == 1
        assert "already_inspected" in model.requests[-1]["input"][-1]["output"]

    asyncio.run(scenario())


def test_fabricated_quotes_cannot_leave_an_answer_marked_supported(tmp_path):
    async def scenario():
        model = Responses(answer("Invented price", quote="not in source"))
        tools = setup(
            tmp_path, model, Browser({"outcome": "ok", "text": "No prices here"})
        )
        result = await create(tools)
        assert result["outcome"] == "unresolved" and not result["answered"]
        assert not result["evidence"] and "Invented price" not in result["answer"]
        assert "Source check unresolved" in tools.finish_answer("A price was invented.")

    asyncio.run(scenario())


def test_truncated_capture_and_display_snapshots_keep_separate_coverage(tmp_path):
    async def scenario():
        browser = Browser(
            {
                "outcome": "ok",
                "text": "US rate",
                "truncated": True,
                "display_state": ["Region: US"],
                "controls": [{"id": "0", "label": "Region", "options": ["EU"]}],
            },
            {
                "outcome": "ok",
                "text": "EU rate",
                "display_state": ["Region: EU"],
                "render_wait": "timeout",
            },
        )
        model = Responses(
            unresolved(display_checked=False),
            call("display_page", control_id="0", option="EU"),
            unresolved(),
        )
        tools = setup(tmp_path, model, browser)
        result = await create(tools)
        assert not result["answered"]
        captures = result["coverage"]["captures"]
        assert captures[0]["incomplete_capture"]
        assert not captures[1]["incomplete_capture"]
        assert captures[0]["display_state"] == ["Region: US"]
        assert captures[1]["display_state"] == ["Region: EU"]
        assert captures[1]["render_wait"] == "timeout"
        assert result["page_work"] == 2

    asyncio.run(scenario())


def test_new_question_context_failure_has_no_stale_answer(tmp_path):
    async def scenario():
        tools = setup(
            tmp_path,
            Responses(answer(quote="Known fact")),
            Browser({"outcome": "ok", "text": "Known fact"}),
        )
        first = await create(tools)
        tools.config = replace(tools.config, context_tokens=9000)
        result = await ask(tools, first["reader_id"], "large question " * 3000)
        assert result["outcome"] == "context_limit"
        assert (
            not result["answered"] and not result["answer"] and not result["evidence"]
        )

    asyncio.run(scenario())


def test_output_limit_retains_machine_notice_even_when_result_cannot_fit(tmp_path):
    async def scenario():
        tools = setup(
            tmp_path,
            Responses(unresolved()),
            Browser({"outcome": "ok", "text": "No billing details", "truncated": True}),
        )
        tools.web.max_output_chars = 150
        result = await create(tools)
        assert result == {"outcome": "output_limit"}
        assert "output limit" in tools.finish_answer("Done")
        assert URL in tools.finish_answer("Done")

    asyncio.run(scenario())


def test_search_context_overflow_never_returns_truncated_ids(tmp_path):
    async def scenario():
        model = Responses(call("search_page", terms=["Other"]))
        tools = setup(
            tmp_path,
            model,
            Browser({"outcome": "ok", "text": long_page()}),
            chunk_tokens=128,
        )
        original = tools._check

        def limited(transcript):
            if transcript and transcript[-1].get("type") == "function_call_output":
                raise ReaderLimit("context_limit")
            original(transcript)

        tools._check = limited
        result = await create(tools)
        assert result["outcome"] == "context_limit"
        assert not result["coverage"]["inspected"]
        assert not any(
            m.get("type") == "function_call_output"
            for m in tools.readers[result["reader_id"]].transcript
        )

    asyncio.run(scenario())


def test_failed_refresh_notice_cannot_be_cleared_by_reusing_stale_content(tmp_path):
    async def scenario():
        browser = Browser(
            {"outcome": "ok", "text": "Old price"},
            {"outcome": "blocked"},
            {"outcome": "ok", "text": "New price"},
        )
        model = Responses(
            answer(quote="Old price"),
            answer(quote="Old price"),
            answer(quote="New price"),
        )
        tools = setup(tmp_path, model, browser)
        first = await create(tools, "Price?")
        await ask(tools, first["reader_id"], "Price?", refresh=True)
        await ask(tools, first["reader_id"], "Price?")
        assert "refresh failed" in tools.finish_answer("Old evidence")
        tools.request_started()
        await ask(tools, first["reader_id"], "Price?", refresh=True)
        assert tools.finish_answer("New evidence") == "New evidence"

    asyncio.run(scenario())


def test_display_failure_survives_exhaustive_captured_text_inspection(tmp_path):
    async def scenario():
        tools = setup(
            tmp_path,
            Responses(
                call("display_page", control_id="render", option="Billing"),
                unresolved(),
            ),
            Browser({"outcome": "ok", "text": "Overview"}),
        )
        tools.web.config = replace(
            tools.web.config, quick=replace(tools.web.config.quick, max_pages=1)
        )
        tools.web.request_started()
        result = await create(tools)
        assert result["coverage"]["inspection"] == "all_captured"
        assert result["coverage"]["display_failures"][0]["outcome"] == "page_limit"
        assert "display operation failed" in tools.finish_answer("Done")

    asyncio.run(scenario())
