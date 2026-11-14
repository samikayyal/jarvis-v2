"""Bounded main-agent Google search, separate from concurrent browser readers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Protocol

from .config import WebSearchConfig
from .web_search import MAX_QUERY_LENGTH


class PageBrowser(Protocol):
    async def search(self, query: str, limit: int) -> dict[str, object]: ...

    async def close(self) -> None: ...


def _browser(
    runtime_dir: Path,
    timeout: float,
    max_chars: int,
    *,
    headed: bool = False,
) -> PageBrowser:
    from .web_browser import BrowserWorker

    return BrowserWorker(runtime_dir, timeout, max_chars, headed=headed)


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
        self._search_lock = asyncio.Lock()
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
            )
            if config.enabled
            else ()
        )

    def request_started(self) -> None:
        if self._browser is not None:
            raise RuntimeError("close the previous web request before resetting")
        self._limits = self.config.quick
        self._queries_used = 0
        self._evidence_chars = 0
        self._worker_failed = False
        self._search_failure: dict[str, str] = {}

    async def close_request(self) -> None:
        browsers = [self._browser] if self._browser is not None else []
        self._browser = None
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

    def _remaining(self) -> dict[str, int]:
        return {
            "queries": max(0, self._limits.max_queries - self._queries_used),
            "evidence_chars": max(
                0, self.config.max_total_evidence_chars - self._evidence_chars
            ),
        }

    def _error(
        self, outcome: str, reason: str, *, stop_reason: str | None = None
    ) -> str:
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

    def _fit_excerpt(
        self, result: dict[str, object], source: dict, key: str, text: str
    ) -> bool:
        source[key] = ""
        capacity = self.max_output_chars - len(_json(result))
        if capacity < 0:
            return False
        source[key] = _json_prefix(text, capacity)
        return True

    def _get_browser(self) -> PageBrowser:
        if self._browser is None:
            self._browser = self._factory(
                self.runtime_dir,
                self.config.operation_timeout_seconds,
                self.config.max_chars_per_page,
            )
        return self._browser

    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        if name != "search_web" or not self.config.enabled:
            raise ValueError("unavailable web tool")
        # One shared browser, block state and evidence allowance per request.
        async with self._search_lock:
            return await self._search(arguments)

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

    async def resume(
        self, continuation: object, *, approved: bool, full_access: bool = False
    ) -> str:
        raise ValueError("web search has no approval continuation")
