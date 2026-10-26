"""Capture the public empty-result DOM through the normal isolated worker."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from jarvis_personal_runtime.web_browser import BrowserWorker


async def main():
    with tempfile.TemporaryDirectory(prefix="jarvis-empty-diagnostic-") as folder:
        root = Path(folder)
        entry = root / "worker.py"
        entry.write_text(
            "from pathlib import Path\n"
            "from jarvis_personal_runtime import web_worker, web_search\n"
            "original = web_search.extract_google_results\n"
            "def capture(html, limit, max_chars):\n"
            "    Path('/tmp/jarvis-empty-20261017.html').write_text(html, encoding='utf-8')\n"
            "    return original(html, limit, max_chars)\n"
            "web_search.extract_google_results = capture\n"
            "raise SystemExit(web_worker.main())\n",
            encoding="utf-8",
        )

        async def factory(*args, **kwargs):
            i = args.index(sys.executable)
            return await asyncio.create_subprocess_exec(
                *args[:i], sys.executable, str(entry), *args[i + 3 :], **kwargs
            )

        worker = BrowserWorker(
            root / "profiles", 60, 8000, headed=True, process_factory=factory
        )
        try:
            result = await worker.search('"jarvis-no-match-70b482f9-20261017-zqxv"', 5)
            print(json.dumps(result), flush=True)
        finally:
            await worker.close()
        assert not list((root / "profiles").iterdir())


asyncio.run(main())
