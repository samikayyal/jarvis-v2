"""Bounded public-page evidence and allowances owned by one ordinary request."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Protocol
from urllib.parse import urldefrag

from .config import WebSearchConfig
from .web_evidence import extraction_metadata
from .web_search import MAX_QUERY_LENGTH

MAX_BATCH_URLS = 5
UNAVAILABLE_NOTICE = (
    "Web access is unavailable for this request; I could not verify current web evidence. "
    "Any answer below is based on existing knowledge, not current web verification."
)
EMPTY_NOTICE = "No useful search results were found."


class PageBrowser(Protocol):
    async def search(self, query: str, limit: int) -> dict[str, object]: ...

    async def read(
        self, url: str, *, wait_for_text: str | None = None
    ) -> dict[str, object]: ...

    async def close(self) -> None: ...


def _browser(
    runtime_dir: Path,
    timeout: float,
    max_chars: int,
    *,
    headed: bool = False,
    rendered: bool = False,
) -> PageBrowser:
    from .web_browser import BrowserWorker

    return BrowserWorker(
        runtime_dir, timeout, max_chars, headed=headed, rendered=rendered
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_prefix(text: str, capacity: int) -> str:
    """Fit a prefix by escaped character cost, without repeated envelope encoding."""
    if len(_json(text)) - 2 <= capacity:
        return text
    for index, character in enumerate(text):
        capacity -= len(_json(character)) - 2
        if capacity < 0:
            return text[:index]
    return text


class WebTools:
    """Quick or moderate research with request-owned cumulative allowances."""

    def __init__(
        self,
        config: WebSearchConfig,
        *,
        runtime_dir: Path,
        max_output_chars: int,
        browser_factory: Callable[..., PageBrowser] | None = None,
        rendered_browser_factory: Callable[..., PageBrowser] | None = None,
    ) -> None:
        if max_output_chars < 2:
            raise ValueError("web output capacity must hold a JSON object")
        self.config = config
        self.runtime_dir = runtime_dir
        self.max_output_chars = max_output_chars
        self._factory = browser_factory or partial(
            _browser, headed=config.browser_mode == "headed"
        )
        self._browser: PageBrowser | None = None
        self._render_factory = rendered_browser_factory or partial(
            _browser, headed=config.browser_mode == "headed", rendered=True
        )
        self._render_browser: PageBrowser | None = None
        self.request_started()
        self.definitions: tuple[dict[str, object], ...] = (
            (
                {
                    "type": "function",
                    "name": "search_web",
                    "description": (
                        "Search Google for bounded organic evidence. Choose quick or "
                        "moderate depth; upgrading preserves all usage and later quick "
                        "calls cannot downgrade. Allowances are shared across this request."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_QUERY_LENGTH,
                            },
                            "depth": {"type": "string", "enum": ["quick", "moderate"]},
                        },
                        "required": ["query", "depth"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "read_web_pages",
                    "description": (
                        "Read public HTML URLs sequentially for cited answers. Choose quick "
                        "or moderate depth; upgrading preserves all usage and later quick "
                        "calls cannot downgrade. Start with 1–2 high-value pages, inspect "
                        "results, then select the next source. Use larger batches only for "
                        "known independent sources. Stop on evidence_limit. Static reads "
                        "come first; rendered mode requires a prior static read of the same "
                        "URL and expected visible text, and consumes another page attempt."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "urls": {
                                "type": "array",
                                "items": {"type": "string", "maxLength": 2048},
                                "minItems": 1,
                                "maxItems": MAX_BATCH_URLS,
                            },
                            "depth": {"type": "string", "enum": ["quick", "moderate"]},
                            "render_mode": {
                                "type": "string",
                                "enum": ["static", "rendered"],
                            },
                            "wait_for_text": {
                                "type": ["string", "null"],
                                "maxLength": 200,
                            },
                        },
                        "required": ["urls", "depth", "render_mode", "wait_for_text"],
                        "additionalProperties": False,
                    },
                },
            )
            if config.enabled
            else ()
        )

    def request_started(self) -> None:
        if self._browser is not None or self._render_browser is not None:
            raise RuntimeError("close the previous web request before resetting")
        self._limits = self.config.quick
        self._pages_used = 0
        self._queries_used = 0
        self._empty_search = False
        self._evidence_chars = 0
        self._has_evidence = False
        self._worker_failed = False
        self._render_worker_failed = False
        self._static_outcomes: dict[str, str] = {}
        self._denied_urls: set[str] = set()
        self._page_failures: dict[str, str] = {}
        self._search_failure: dict[str, str] = {}
        self._pages_read = 0
        self._limit_stops: set[str] = set()
        self._render_timeouts = 0

    async def close_request(self) -> None:
        browsers = [b for b in (self._browser, self._render_browser) if b is not None]
        self._browser = self._render_browser = None
        if browsers:

            async def close_all():
                results = await asyncio.gather(
                    *(b.close() for b in browsers), return_exceptions=True
                )
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

            cleanup = asyncio.create_task(close_all())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError

    def finish_answer(self, text: str) -> str:
        access_failed = bool(self._search_failure or self._page_failures)
        if (
            not access_failed
            and not self._limit_stops
            and not self._render_timeouts
            and not (self._empty_search and not self._has_evidence)
        ):
            return text
        facts = []
        if self._search_failure:
            facts.append(
                {
                    "blocked": "Google search was blocked.",
                    "timeout": "Google search timed out.",
                    "extraction_failed": "Google search results could not be extracted.",
                }.get(self._search_failure["outcome"], "Google search was unavailable.")
            )
        if self._page_failures:
            counts = {
                reason: list(self._page_failures.values()).count(reason)
                for reason in sorted(set(self._page_failures.values()))
            }
            facts.append(
                "Page access: "
                + ", ".join(
                    f"{count} {reason.replace('_', ' ')}"
                    for reason, count in counts.items()
                )
                + "."
            )
        if self._pages_read:
            noun = "page excerpt" if self._pages_read == 1 else "page excerpts"
            facts.append(f"Read {self._pages_read} {noun}.")
        for reason, sentence in (
            ("evidence_limit", "The text allowance prevented further web reads."),
            ("page_limit", "The page allowance prevented further page reads."),
            ("query_limit", "The search allowance prevented further searches."),
            ("output_limit", "The tool output limit restricted returned evidence."),
        ):
            if reason in self._limit_stops:
                facts.append(sentence)
        if self._render_timeouts:
            facts.append(
                "Requested dynamic content did not appear before the rendering wait ended."
            )
        if access_failed and not self._has_evidence:
            facts.insert(0, UNAVAILABLE_NOTICE)
        elif access_failed:
            facts.append("Web coverage is incomplete.")
        elif self._empty_search and not self._has_evidence:
            facts.insert(0, EMPTY_NOTICE)
        notice = " ".join(facts)
        answer = f"{notice}\n\n{text}"
        if len(answer) <= self.max_output_chars or len(text) > self.max_output_chars:
            return answer
        # The runtime still rejects an oversized provider answer. Reserve room
        # for our mandatory disclosure when the provider answer itself fits.
        suffix = "\n[Answer shortened to fit the output limit.]"
        capacity = self.max_output_chars - len(notice) - len(suffix) - 2
        if capacity <= 0:
            return notice[: self.max_output_chars]
        excerpt = text[:capacity]
        if " " in excerpt:
            excerpt = excerpt.rsplit(" ", 1)[0]
        return f"{notice}\n\n{excerpt.rstrip()}{suffix}"

    def _remaining(self) -> dict[str, int]:
        return {
            "queries": max(0, self._limits.max_queries - self._queries_used),
            "pages": max(0, self._limits.max_pages - self._pages_used),
            "evidence_chars": max(
                0, self.config.max_total_evidence_chars - self._evidence_chars
            ),
        }

    def _error(
        self, outcome: str, reason: str, *, stop_reason: str | None = None
    ) -> str:
        if stop_reason:
            self._limit_stops.add(stop_reason)
        for result in (
            {
                "outcome": outcome,
                "reason": reason,
                "remaining": self._remaining(),
                **({"stop_reason": stop_reason} if stop_reason else {}),
            },
            {"outcome": outcome},
            {},
        ):
            encoded = _json(result)
            if len(encoded) <= self.max_output_chars:
                return encoded
        raise AssertionError("JSON capacity validated at construction")

    def _fit_text(
        self, result: dict[str, object], pages: list[dict[str, object]]
    ) -> bool:
        """Allocate output space once, retaining metadata for every source."""
        if len(_json(result)) <= self.max_output_chars:
            return True
        excerpts = [(page, dict(page)) for page in pages if page["text"]]
        for page, _ in excerpts:
            page.update(
                text="",
                outcome="limit_reached",
                truncated=True,
                more_content_available=True,
                stop_reason="output_limit",
            )
        self._limit_stops.add("output_limit")
        capacity = self.max_output_chars - len(_json(result))
        if capacity < 0:
            return False
        for index, (page, original) in enumerate(excerpts):
            share = capacity // (len(excerpts) - index)
            empty_size = len(_json(page))
            if len(_json(original)) - empty_size <= share:
                page.clear()
                page.update(original)
            else:
                page["text"] = _json_prefix(str(original["text"]), share)
                if page["text"]:
                    page["outcome"] = original["outcome"]
            capacity -= len(_json(page)) - empty_size
        return True

    def _fit_excerpt(
        self, result: dict[str, object], source: dict, key: str, text: str
    ) -> bool:
        source[key] = ""
        capacity = self.max_output_chars - len(_json(result))
        if capacity < 0:
            return False
        source[key] = _json_prefix(text, capacity)
        return True

    def _get_browser(self, *, rendered: bool = False) -> PageBrowser:
        if rendered:
            if self._render_browser is None:
                self._render_browser = self._render_factory(
                    self.runtime_dir,
                    self.config.operation_timeout_seconds,
                    self.config.max_chars_per_page,
                )
            return self._render_browser
        if self._browser is None:
            self._browser = self._factory(
                self.runtime_dir,
                self.config.operation_timeout_seconds,
                self.config.max_chars_per_page,
            )
        return self._browser

    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        from .web_browser import validate_public_url

        if name == "search_web" and self.config.enabled:
            return await self._search(arguments)
        if name != "read_web_pages" or not self.config.enabled:
            raise ValueError("unavailable web tool")
        urls = arguments.get("urls")
        mode = arguments.get("render_mode", "static")
        wait_for_text = arguments.get("wait_for_text")
        if (
            not {"urls", "depth"} <= set(arguments)
            or set(arguments) - {"urls", "depth", "render_mode", "wait_for_text"}
            or mode not in ("static", "rendered")
            or (mode == "static" and wait_for_text is not None)
            or (
                mode == "rendered"
                and (
                    not isinstance(wait_for_text, str)
                    or not 1 <= len(wait_for_text.strip()) <= 200
                )
            )
            or arguments.get("depth") not in ("quick", "moderate")
            or not isinstance(urls, list)
            or not 1 <= len(urls) <= MAX_BATCH_URLS
        ):
            return self._error(
                "invalid_input",
                "Use quick/moderate depth and 1–5 public URLs; rendered mode requires 1–200 characters of expected visible text.",
            )
        try:
            for url in urls:
                if not isinstance(url, str):
                    raise TypeError("URLs must be strings")
                canonical = urldefrag(validate_public_url(url))[0]
                if mode == "rendered" and (
                    self._static_outcomes.get(canonical)
                    not in {"ok", "extraction_failed"}
                    or canonical in self._denied_urls
                ):
                    return self._error(
                        "invalid_input",
                        "Rendered reads require a prior static read of the same URL without an access block.",
                    )
        except (ValueError, TypeError):
            return self._error(
                "blocked", "Only public HTTP(S) URLs without credentials are allowed."
            )

        if arguments["depth"] == "moderate":
            self._limits = self.config.moderate

        # Reserve source identities for the whole batch before doing any network work.
        pages: list[dict[str, object]] = [
            {
                "requested_url": url,
                "url": url,
                "title": "",
                "text": "",
                "outcome": "unattempted",
                "evidence_type": "page",
                "reason": "allowance",
            }
            for url in urls
        ]
        result: dict[str, object] = {
            "outcome": "limit_reached",
            "pages": pages,
            "remaining": self._remaining(),
        }
        if len(_json(result)) > self.max_output_chars:
            return self._error(
                "limit_reached",
                "Batch source identities exceed the runtime output allowance; use a smaller batch.",
                stop_reason="output_limit",
            )

        for index, url in enumerate(urls):
            batch_chars = sum(len(str(page["text"])) for page in pages)
            evidence_left = self._remaining()["evidence_chars"] - batch_chars
            if not self._remaining()["pages"] or evidence_left <= 0:
                stop = "evidence_limit" if evidence_left <= 0 else "page_limit"
                self._limit_stops.add(stop)
                result["stop_reason"] = stop
                for pending in pages[index:]:
                    pending["reason"] = stop
                break
            canonical = urldefrag(validate_public_url(url))[0]
            if canonical in self._denied_urls:
                pages[index]["reason"] = "previous_access_block"
                continue
            if (
                self._render_worker_failed
                if mode == "rendered"
                else self._worker_failed
            ):
                pages[index]["reason"] = "worker_unavailable"
                continue
            self._pages_used += 1
            try:
                browser = self._get_browser(rendered=mode == "rendered")
                page = (
                    await browser.read(url, wait_for_text=wait_for_text)
                    if mode == "rendered"
                    else await browser.read(url)
                )
            except (OSError, RuntimeError, ValueError):
                page = {"outcome": "unavailable", "reason": "Browser worker failed."}
            outcome = str(page.get("outcome", "extraction_failed"))
            text = str(page.get("text", "")) if outcome == "ok" else ""
            original_length = len(text)
            text = text[: min(self.config.max_chars_per_page, evidence_left)]
            if outcome == "ok" and not text.strip():
                outcome = "extraction_failed"
                text = ""
            candidate: dict[str, object] = {
                "requested_url": url,
                "url": str(page.get("url") or url),
                "title": str(page.get("title", "")),
                "text": text,
                "outcome": outcome,
                "evidence_type": "page",
            }
            if page.get("reason"):
                candidate["reason"] = str(page["reason"])
            if outcome == "ok":
                candidate.update(extraction_metadata(page, text))
                if original_length > len(text):
                    candidate.update(
                        truncated=True,
                        more_content_available=True,
                        stop_reason=(
                            "evidence_limit"
                            if evidence_left
                            < min(original_length, self.config.max_chars_per_page)
                            else "page_text_limit"
                        ),
                    )
                self._render_timeouts += candidate["render_wait"] == "timeout"
            else:
                self._page_failures[canonical] = outcome
            if mode == "static":
                self._static_outcomes[canonical] = outcome
            if outcome in {"blocked", "unsupported"}:
                self._denied_urls.add(canonical)
            elif outcome == "ok":
                self._page_failures.pop(canonical, None)
            pages[index] = candidate
            result["remaining"] = self._remaining()
            failed = outcome == "unavailable" or page.get("usable") is False
            if mode == "rendered":
                self._render_worker_failed |= failed
            else:
                self._worker_failed |= failed
        # Late stop reasons change serialized size. Fit the final envelope before
        # charging evidence, reserving the longest possible aggregate outcome.
        result["outcome"] = "extraction_failed"
        result["remaining"] = self._remaining()
        if not self._fit_text(result, pages):
            return self._error(
                "limit_reached",
                "Final source metadata exceeds output allowance.",
                stop_reason="output_limit",
            )
        batch_chars = sum(len(str(page["text"])) for page in pages)
        outcomes = {page["outcome"] for page in pages}
        if outcomes == {"ok"}:
            result["outcome"] = "ok"
        elif "ok" in outcomes:
            result["outcome"] = "partial"
        elif outcomes == {"unattempted"} and any(
            page.get("reason") == "previous_access_block" for page in pages
        ):
            result["outcome"] = "blocked"
        elif outcomes == {"unattempted"} and (
            self._render_worker_failed if mode == "rendered" else self._worker_failed
        ):
            result["outcome"] = "unavailable"
        elif outcomes <= {"unattempted", "limit_reached"}:
            result["outcome"] = "limit_reached"
        elif len(outcomes) == 1:
            result["outcome"] = pages[0]["outcome"]
        else:
            result["outcome"] = "partial"
        result["remaining"] = self._remaining()
        result["remaining"]["evidence_chars"] -= batch_chars
        encoded = _json(result)
        if len(encoded) > self.max_output_chars:
            return self._error(
                "limit_reached",
                "Runtime output allowance exhausted.",
                stop_reason="output_limit",
            )
        self._pages_read += sum(
            page["outcome"] == "ok" and bool(page["text"]) for page in pages
        )
        self._evidence_chars += batch_chars
        self._has_evidence |= bool(batch_chars)
        return encoded

    async def _search(self, arguments: dict[str, object]) -> str:
        from .web_browser import PublicUrlError, validate_public_url

        query = arguments.get("query")
        if (
            set(arguments) != {"query", "depth"}
            or arguments.get("depth") not in ("quick", "moderate")
            or not isinstance(query, str)
            or not query.strip()
            or len(query) > MAX_QUERY_LENGTH
        ):
            return self._error(
                "invalid_input", "Use one nonempty query and quick or moderate depth."
            )
        if arguments["depth"] == "moderate":
            self._limits = self.config.moderate
        if self._search_failure.get("outcome") == "blocked":
            return self._error(
                "blocked",
                self._search_failure.get("reason")
                or "Google search is blocked for this request.",
            )
        if self._worker_failed:
            return self._error("unavailable", "Browser worker is unavailable.")
        if not self._remaining()["queries"] or not self._remaining()["evidence_chars"]:
            stop = (
                "evidence_limit"
                if not self._remaining()["evidence_chars"]
                else "query_limit"
            )
            return self._error(
                "limit_reached", "Search allowance exhausted.", stop_reason=stop
            )
        result: dict[str, object] = {
            "outcome": "extraction_failed",
            "query": query,
            "results": [],
            "remaining": self._remaining(),
        }
        if len(_json(result)) > self.max_output_chars:
            return self._error(
                "limit_reached",
                "Query exceeds output allowance.",
                stop_reason="output_limit",
            )
        self._queries_used += 1
        try:
            response = await self._get_browser().search(
                query, self.config.results_per_query
            )
        except (OSError, RuntimeError, ValueError):
            response = {"outcome": "unavailable"}
        outcome = response.get("outcome")
        if outcome not in {"ok", "no_results"}:
            self._search_failure = {
                "outcome": str(outcome),
                "reason": str(response.get("reason", ""))[:512],
            }
        if outcome not in {
            "ok",
            "no_results",
            "blocked",
            "extraction_failed",
            "unavailable",
        }:
            outcome = "unavailable" if outcome == "timeout" else "extraction_failed"
        self._worker_failed |= (
            outcome == "unavailable" or response.get("usable") is False
        )
        sources: list[dict[str, str]] = []
        raw = response.get("results", [])
        if outcome == "ok" and isinstance(raw, list):
            for item in raw[: self.config.results_per_query]:
                if not isinstance(item, dict):
                    continue
                try:
                    url = validate_public_url(item.get("url"))
                except (PublicUrlError, TypeError):
                    continue
                title = str(item.get("title", ""))[:512]
                if not title.strip():
                    continue
                sources.append(
                    {
                        "title": title,
                        "url": url,
                        "snippet": str(item.get("snippet", ""))[
                            : self.config.max_chars_per_page
                        ],
                        "evidence_type": "search_result",
                    }
                )
        if outcome == "ok" and not sources:
            outcome = "extraction_failed"
            self._search_failure = {"outcome": outcome, "reason": "No valid sources"}
        self._empty_search |= outcome == "no_results"
        # Reserve the longest possible outcome while fitting sources.
        result.update(outcome="extraction_failed", results=[])
        accepted: list[dict[str, str]] = []
        result["results"] = accepted
        for source in sources:
            # Titles are evidence too. Source identity is either returned intact
            # or omitted; never truncate a URL to make the JSON fit.
            available = self._remaining()["evidence_chars"] - len(source["title"])
            if available < 0:
                break
            snippet = source["snippet"][:available]
            source["snippet"] = ""
            accepted.append(source)
            result["remaining"] = self._remaining()
            if not self._fit_excerpt(result, source, "snippet", snippet):
                accepted.pop()
                break
            self._evidence_chars += len(source["title"]) + len(source["snippet"])
        self._has_evidence |= bool(accepted)
        result["outcome"] = outcome
        if sources and not accepted:
            result["outcome"] = "limit_reached"
        elif len(accepted) < len(sources):
            result["outcome"] = "partial"
        result["remaining"] = self._remaining()
        reason = response.get("reason")
        if outcome != "ok" and isinstance(reason, str) and reason:
            result["reason"] = reason[:512]
            if len(_json(result)) > self.max_output_chars:
                del result["reason"]
        return _json(result)

    async def resume(self, continuation: object, *, approved: bool) -> str:
        raise ValueError("web reads have no approval continuation")
