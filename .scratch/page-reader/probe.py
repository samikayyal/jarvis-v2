"""Exercise the configured provider and reader without changing the live service."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from jarvis_personal_runtime.config import WebReaderConfig, load_runtime_config
from jarvis_personal_runtime.page_reader import PageReaderTools
from jarvis_personal_runtime.responses import OpenAIRawResponsesAdapter
from jarvis_personal_runtime.web_tools import WebTools


class Trace:
    def record(self, event, payload):
        if event in {"page_reader", "responses_sdk_error"}:
            print(json.dumps(payload), flush=True)
        if event == "responses_parsed":
            print(
                json.dumps({"reader_output": payload["response"].get("output")}),
                flush=True,
            )


async def main():
    base = Path("/home/samik/Projects/Jarvis")
    loaded = load_runtime_config(
        base / "other/runtime", config_path=base / "other/config/jarvis.toml"
    )
    adapter = OpenAIRawResponsesAdapter.from_api_key(
        loaded.secrets.openai_api_key, trace=Trace()
    )
    with tempfile.TemporaryDirectory(prefix="jarvis-reader-probe-") as directory:
        reader = PageReaderTools(
            WebTools(
                loaded.config.web_search,
                runtime_dir=Path(directory),
                max_output_chars=12000,
            ),
            WebReaderConfig(),
            adapter.without_retries(),
            trace=Trace(),
        )
        try:
            result = await reader.execute(
                "read_web_page", {"url": sys.argv[1], "query": sys.argv[2]}
            )
            print(result, flush=True)
            for state in reader.readers.values():
                for chunk in state.chunks:
                    if (
                        chunk["id"] in state.inspected
                        and "e2-standard-2" in chunk["text"]
                    ):
                        start = chunk["text"].index("e2-standard-2")
                        print(
                            json.dumps(
                                {
                                    "source_excerpt": chunk["text"][
                                        max(0, start - 800) : start + 500
                                    ]
                                }
                            ),
                            flush=True,
                        )
        finally:
            await reader.close_request()
            await adapter.close()


asyncio.run(main())
