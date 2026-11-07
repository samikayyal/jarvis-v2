"""Offline regression target: repair a bad quote before concluding unresolved.

Run from the repository root with uv run. Expected to fail before a repair fix.
No network requests or production mutations.
"""

import asyncio
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.personal_runtime.test_page_reader import (  # noqa: E402
    Browser,
    Responses,
    answer,
    create,
    setup,
)


async def main():
    with tempfile.TemporaryDirectory() as directory:
        responses = Responses(
            answer(text="Doha is listed", quote="Doha Qatar"),
            answer(text="Doha is listed", quote="Doha, Qatar"),
        )
        tools = setup(
            Path(directory),
            responses,
            Browser({"outcome": "ok", "text": "Doha, Qatar"}),
        )
        result = await create(tools, "Is Doha listed?")
        print({"outcome": result["outcome"], "calls": len(responses.requests),
               "gaps": result["gaps"]})
        assert result["answered"], "Reader stopped without offering quote repair"
        assert len(responses.requests) == 2
        assert "quote" in str(responses.requests[1]["input"][-1]).lower()


asyncio.run(main())
