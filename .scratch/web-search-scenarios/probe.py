"""Bounded, credential-free web scenarios; no model or messaging calls."""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from jarvis_personal_runtime.config import WebSearchConfig
from jarvis_personal_runtime.web_tools import WebTools


async def main(output):
    report = {"scenarios": [], "cleanup": []}
    blocked = False
    with tempfile.TemporaryDirectory(prefix="jarvis-web-scenarios-") as directory:
        root = Path(directory)
        tool = WebTools(
            WebSearchConfig(
                enabled=True, browser_mode="headed", operation_timeout_seconds=60
            ),
            runtime_dir=root,
            max_output_chars=65536,
        )

        async def call(label, name, arguments):
            nonlocal blocked
            start = time.monotonic()
            result = json.loads(await tool.execute(name, arguments))
            record = {
                "label": label,
                "tool": name,
                "arguments": arguments,
                "seconds": round(time.monotonic() - start, 2),
                "result": result,
            }
            report["scenarios"].append(record)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {
                        "label": label,
                        "seconds": record["seconds"],
                        "outcome": result.get("outcome"),
                        "reason": result.get("reason"),
                        "sources": [
                            {
                                "title": x.get("title"),
                                "url": x.get("url"),
                                "outcome": x.get("outcome"),
                                "reason": x.get("reason"),
                            }
                            for x in result.get("results", result.get("pages", []))
                        ],
                        "remaining": result.get("remaining"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if name == "search_web" and result.get("outcome") == "blocked":
                blocked = True
            return result

        async def reset():
            await tool.close_request()
            clean = not list(root.iterdir())
            report["cleanup"].append(clean)
            assert clean, "browser profile leaked"
            tool.request_started()

        try:
            result = await call(
                "official quick lookup",
                "search_web",
                {"query": "site:iana.org example domains", "depth": "quick"},
            )
            if result.get("results"):
                await call(
                    "open returned search link",
                    "read_web_pages",
                    {"urls": [result["results"][0]["url"]], "depth": "quick"},
                )
            await call(
                "quick query ceiling",
                "search_web",
                {"query": "this query must not reach Google", "depth": "quick"},
            )
            await reset()
            for label, query in [
                (
                    "technical comparison",
                    "site:docs.python.org asyncio TaskGroup gather",
                ),
                ("Arabic query", "البنك المركزي الأردني أسعار العملات"),
                (
                    "fresh release lookup",
                    "site:python.org latest Python release October 2026",
                ),
            ]:
                if blocked:
                    break
                await call(label, "search_web", {"query": query, "depth": "moderate"})
            await reset()
            if not blocked:
                await call(
                    "empty exact phrase",
                    "search_web",
                    {
                        "query": '"jarvis-no-match-70b482f9-20261017-zqxv"',
                        "depth": "quick",
                    },
                )
            await reset()
            await call(
                "public HTML and non-HTML batch",
                "read_web_pages",
                {
                    "urls": [
                        "https://www.iana.org/help/example-domains",
                        "https://docs.python.org/3/library/asyncio-task.html",
                        "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
                    ],
                    "depth": "moderate",
                },
            )
            await call(
                "private address rejection",
                "read_web_pages",
                {"urls": ["http://127.0.0.1/"], "depth": "quick"},
            )
        finally:
            await reset()
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    print(json.dumps({"all_profiles_cleaned": all(report["cleanup"])}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args().output))
