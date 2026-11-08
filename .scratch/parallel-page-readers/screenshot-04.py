"""Run the existing canvas fixture against the installed candidate's public tools.

Execute as the runtime user with the locked test environment. The operator
authorized reuse of the existing runtime key for ticket 04. No secret is printed
or persisted by this helper.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BASE = Path("/home/samik/Projects/Jarvis")
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--sha", default="551ed852b16f17f87e149feae1690da39114fc5b")
parser.add_argument(
    "--tests", type=Path, default=BASE / "code/parallel-readers-test-551ed85"
)
args = parser.parse_args()
assert len(args.sha) == 40 and all(c in "0123456789abcdef" for c in args.sha)
CANDIDATE = BASE / "code/releases" / args.sha
sys.path.insert(0, str(CANDIDATE / ".venv/lib/python3.14/site-packages"))

import pytest

import jarvis_personal_runtime
from jarvis_personal_runtime.config import WebReaderConfig, load_runtime_config
from jarvis_personal_runtime.playwright_reader import PlaywrightReaderTools
from jarvis_personal_runtime.responses import OpenAIRawResponsesAdapter

sys.path.insert(0, str(args.tests))
from tests.personal_runtime.test_playwright_browser import public_fixture

assert Path(jarvis_personal_runtime.__file__).is_relative_to(CANDIDATE / ".venv")
loaded = load_runtime_config(
    BASE / "other/runtime", config_path=BASE / "other/config/jarvis.toml"
)


async def verify(url):
    adapter = OpenAIRawResponsesAdapter.from_api_key(loaded.secrets.openai_api_key)
    tools = PlaywrightReaderTools(WebReaderConfig(), adapter.without_retries())
    started = time.monotonic()
    try:
        result = json.loads(
            await tools.execute(
                "start_browser_reader",
                {
                    "url": url + "visual",
                    "prompt": "Request a screenshot. Read the large code drawn on the canvas. Return that exact code and source URL; do not guess if you cannot see it.",
                },
            )
        )
        # The public wait operation returns after at most 30 seconds even while
        # work continues. Respect the configured 120-second assignment budget.
        for _ in range(5):
            result = json.loads(
                await tools.execute(
                    "wait_browser_reader",
                    {
                        "reader_id": result["reader_id"],
                        "seconds": 30,
                    },
                )
            )
            if result["state"] != "running":
                break
        assert result["state"] == "completed", result
        assert "ORCHID 7391" in result["findings"], result
        assert url + "visual" in result["findings"], result
        print(
            json.dumps(
                {
                    "model": "gpt-6-luna",
                    "model_turns": result["model_turns"],
                    "canvas_code_read": True,
                    "elapsed_seconds": time.monotonic() - started,
                    "findings": result["findings"],
                }
            )
        )
    finally:
        await tools.close_request()
        await adapter.close()


with pytest.MonkeyPatch.context() as patch, public_fixture(patch) as fixture_url:
    asyncio.run(verify(fixture_url))
