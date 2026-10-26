"""One bounded anonymous static/rendered comparison; no model or messaging calls."""

import asyncio
import json
import tempfile
from pathlib import Path

from jarvis_personal_runtime.config import WebSearchConfig
from jarvis_personal_runtime.web_tools import WebTools


async def main():
    with tempfile.TemporaryDirectory(prefix="jarvis-render-probe-") as temporary:
        tool = WebTools(
            WebSearchConfig(
                enabled=True, browser_mode="headed", operation_timeout_seconds=60
            ),
            runtime_dir=Path(temporary),
            max_output_chars=65536,
        )
        url = "https://quotes.toscrape.com/js/"
        try:
            for mode in ("static", "rendered"):
                raw = await tool.execute(
                    "read_web_pages",
                    {
                        "urls": [url],
                        "depth": "moderate",
                        "render_mode": mode,
                        "wait_for_text": "Albert Einstein"
                        if mode == "rendered"
                        else None,
                    },
                )
                result = json.loads(raw)
                for page in result.get("pages", []):
                    text = page.pop("text", "")
                    page["text_length"] = len(text)
                    page["expected_content_present"] = "Albert Einstein" in text
                    page["excerpt"] = text[:160]
                print(json.dumps(result), flush=True)
                if mode == "static" and result["pages"][0]["outcome"] not in {
                    "ok",
                    "extraction_failed",
                }:
                    break
        finally:
            await tool.close_request()
        print("profiles_remaining", len(list(Path(temporary).iterdir())))


asyncio.run(main())
