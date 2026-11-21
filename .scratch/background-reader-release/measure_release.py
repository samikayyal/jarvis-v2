"""Time one real reader release with the installed runtime and a local page fixture."""

import argparse
import asyncio
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from time import perf_counter_ns
from unittest.mock import patch

import jarvis_personal_runtime
from jarvis_personal_runtime.config import WebReaderConfig
from jarvis_personal_runtime.playwright_reader import PlaywrightReaderTools


class WaitingModel:
    def __init__(self):
        self.started = asyncio.Event()

    async def create(self, request, *, timeout):
        self.started.set()
        await asyncio.Future()


class FixturePatch(ExitStack):
    def setattr(self, obj, name, value):
        self.enter_context(patch.object(obj, name, value))


async def trial(url):
    model = WaitingModel()
    tools = PlaywrightReaderTools(WebReaderConfig(), model)
    try:
        start = json.loads(
            await tools.execute(
                "start_browser_reader", {"url": url, "prompt": "Read this fixture."}
            )
        )
        await asyncio.wait_for(model.started.wait(), timeout=45)
        reader_id = start["reader_id"]
        browser = tools.readers[reader_id].browser
        driver = browser._playwright._impl_obj._connection._transport._proc
        proxy = browser._proxy._thread
        begin = perf_counter_ns()
        result = await tools.execute("release_browser_reader", {"reader_id": reader_id})
        returned = perf_counter_ns()
        assert json.loads(result) == {"reader_id": reader_id, "state": "released"}
        expired = await tools.execute("browser_reader_status", {"reader_id": reader_id})
        assert json.loads(expired)["error"] == "expired_reader_id"
        await tools.close_request()
        drained = perf_counter_ns()
        assert driver.returncode is not None and not proxy.is_alive()
        return {
            "release_ms": (returned - begin) / 1e6,
            "release_through_drain_ms": (drained - begin) / 1e6,
        }
    finally:
        await tools.close_request()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("trial", type=int)
    args = parser.parse_args()
    assert (
        Path(jarvis_personal_runtime.__file__)
        .resolve()
        .is_relative_to(args.release / ".venv")
    )
    sys.path.insert(0, str(args.release / "tests/personal_runtime"))
    from test_playwright_browser import public_fixture

    with FixturePatch() as fixture_patch, public_fixture(fixture_patch) as url:
        print(
            json.dumps(
                {
                    "sha": args.release.name,
                    "trial": args.trial,
                    **asyncio.run(trial(url)),
                }
            ),
            flush=True,
        )
