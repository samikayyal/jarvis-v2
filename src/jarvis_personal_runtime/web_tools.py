"""Bounded public-page evidence and allowances owned by one ordinary request."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .config import WebSearchConfig

MAX_BATCH_URLS = 20
UNAVAILABLE_NOTICE = (
    "Web access is unavailable for this request; I could not verify these pages. "
    "Any answer below is based on existing knowledge, not current web verification."
)
PARTIAL_NOTICE = "Some requested pages could not be read; web coverage is incomplete."


class PageBrowser(Protocol):
    async def read(self, url: str) -> dict[str, object]: ...

    async def close(self) -> None: ...


def _browser(runtime_dir: Path, timeout: float, max_chars: int) -> PageBrowser:
    from .web_browser import BrowserWorker

    return BrowserWorker(runtime_dir, timeout, max_chars)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class WebTools:
    """Quick reads only; loading a skill never resets this request's allowance."""

    def __init__(
        self,
        config: WebSearchConfig,
        *,
        runtime_dir: Path,
        max_output_chars: int,
        browser_factory: Callable[..., PageBrowser] = _browser,
    ) -> None:
        if max_output_chars < 2:
            raise ValueError("web output capacity must hold a JSON object")
        self.config = config
        self.runtime_dir = runtime_dir
        self.max_output_chars = max_output_chars
        self._factory = browser_factory
        self._browser: PageBrowser | None = None
        self.request_started()
        self.definitions: tuple[dict[str, object], ...] = (
            (
                {
                    "type": "function",
                    "name": "read_web_pages",
                    "description": (
                        "Read public HTML URLs sequentially for cited answers. Only quick "
                        "depth is available. The page allowance is shared across this request."
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
                            "depth": {"type": "string", "enum": ["quick"]},
                        },
                        "required": ["urls", "depth"],
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
        self._pages_used = 0
        self._evidence_chars = 0
        self._access_failed = False
        self._has_evidence = False
        self._worker_failed = False

    async def close_request(self) -> None:
        browser, self._browser = self._browser, None
        if browser is not None:
            cleanup = asyncio.create_task(browser.close())
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
        if not self._access_failed:
            return text
        notice = PARTIAL_NOTICE if self._has_evidence else UNAVAILABLE_NOTICE
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
            "queries": self.config.quick.max_queries,
            "pages": max(0, self.config.quick.max_pages - self._pages_used),
            "evidence_chars": max(
                0, self.config.max_total_evidence_chars - self._evidence_chars
            ),
        }

    def _error(self, outcome: str, reason: str) -> str:
        for result in (
            {"outcome": outcome, "reason": reason, "remaining": self._remaining()},
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
        """Shorten only excerpts, keeping every returned source identity intact."""
        while len(_json(result)) > self.max_output_chars:
            page = max(pages, key=lambda item: len(str(item["text"])))
            text = str(page["text"])
            if not text:
                return False
            low, high = 0, len(text) - 1
            page["text"] = ""
            if len(_json(result)) <= self.max_output_chars:
                while low < high:
                    middle = (low + high + 1) // 2
                    page["text"] = text[:middle]
                    if len(_json(result)) <= self.max_output_chars:
                        low = middle
                    else:
                        high = middle - 1
                page["text"] = text[:low]
            if not page["text"] and page["outcome"] == "ok":
                page["outcome"] = "limit_reached"
        return True

    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        from .web_browser import validate_public_url

        if name != "read_web_pages" or not self.config.enabled:
            raise ValueError("unavailable web tool")
        urls = arguments.get("urls")
        if (
            set(arguments) != {"urls", "depth"}
            or arguments.get("depth") != "quick"
            or not isinstance(urls, list)
            or not 1 <= len(urls) <= MAX_BATCH_URLS
        ):
            return self._error("invalid_input", "Use quick depth and 1–20 public URLs.")
        try:
            for url in urls:
                if not isinstance(url, str):
                    raise TypeError("URLs must be strings")
                validate_public_url(url)
        except (ValueError, TypeError):
            return self._error(
                "blocked", "Only public HTTP(S) URLs without credentials are allowed."
            )

        # Reserve source identities for the whole batch before doing any network work.
        pages: list[dict[str, object]] = [
            {
                "requested_url": url,
                "url": url,
                "title": "",
                "text": "",
                "outcome": "unattempted",
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
            )

        for index, url in enumerate(urls):
            batch_chars = sum(len(str(page["text"])) for page in pages)
            evidence_left = self._remaining()["evidence_chars"] - batch_chars
            if not self._remaining()["pages"] or evidence_left <= 0:
                break
            if self._worker_failed:
                pages[index]["reason"] = "worker_unavailable"
                continue
            self._pages_used += 1
            try:
                if self._browser is None:
                    self._browser = self._factory(
                        self.runtime_dir,
                        self.config.operation_timeout_seconds,
                        self.config.max_chars_per_page,
                    )
                page = await self._browser.read(url)
            except (OSError, RuntimeError, ValueError):
                page = {"outcome": "unavailable", "reason": "Browser worker failed."}
            outcome = str(page.get("outcome", "extraction_failed"))
            text = str(page.get("text", "")) if outcome == "ok" else ""
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
            }
            if page.get("reason"):
                candidate["reason"] = str(page["reason"])
            pages[index] = candidate
            result["remaining"] = self._remaining()
            self._access_failed |= outcome not in {"ok", "limit_reached"}
            self._worker_failed |= (
                outcome == "unavailable" or page.get("usable") is False
            )
            # Metadata is never clipped into a different source identity.
            if not self._fit_text(result, pages):
                pages[index] = {
                    "requested_url": url,
                    "url": url,
                    "title": "",
                    "text": "",
                    "outcome": "limit_reached",
                    "reason": "source_metadata_exceeds_output",
                }
                if not self._fit_text(result, pages):
                    return self._error(
                        "limit_reached",
                        "Source metadata exceeds the runtime output allowance.",
                    )

        batch_chars = sum(len(str(page["text"])) for page in pages)
        self._evidence_chars += batch_chars
        self._has_evidence |= bool(batch_chars)
        outcomes = {page["outcome"] for page in pages}
        if outcomes == {"ok"}:
            result["outcome"] = "ok"
        elif "ok" in outcomes:
            result["outcome"] = "partial"
        elif outcomes == {"unattempted"} and self._worker_failed:
            result["outcome"] = "unavailable"
        elif outcomes <= {"unattempted", "limit_reached"}:
            result["outcome"] = "limit_reached"
        elif len(outcomes) == 1:
            result["outcome"] = pages[0]["outcome"]
        else:
            result["outcome"] = "partial"
        result["remaining"] = self._remaining()
        encoded = _json(result)
        if len(encoded) > self.max_output_chars:
            return self._error("limit_reached", "Runtime output allowance exhausted.")
        return encoded

    async def resume(self, continuation: object, *, approved: bool) -> str:
        raise ValueError("web reads have no approval continuation")
