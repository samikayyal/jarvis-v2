"""One bounded real search and selected page read through WebTools."""

import asyncio
import argparse
import json
import tempfile
import time
from pathlib import Path

from jarvis_personal_runtime.config import WebSearchConfig
from jarvis_personal_runtime.web_tools import WebTools


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--query", default="site:iana.org example domains")
    args = parser.parse_args()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="jarvis-headed-tool-probe-") as folder:
        web = WebTools(
            WebSearchConfig(
                enabled=True,
                browser_mode="headed",
                operation_timeout_seconds=args.timeout,
            ),
            runtime_dir=Path(folder),
            max_output_chars=20000,
        )
        try:
            search = json.loads(
                await web.execute(
                    "search_web",
                    {"query": args.query, "depth": "quick"},
                )
            )
            print(
                json.dumps({"search": search, "elapsed": time.monotonic() - started}),
                flush=True,
            )
            if search["outcome"] == "ok" and search.get("results"):
                read = json.loads(
                    await web.execute(
                        "read_web_pages",
                        {"urls": [search["results"][0]["url"]], "depth": "quick"},
                    )
                )
                print(json.dumps({"read": read}), flush=True)
        finally:
            await web.close_request()
        print(json.dumps({"remaining_profiles": list(Path(folder).iterdir())}))


asyncio.run(main())
