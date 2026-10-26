import asyncio
import json
from pathlib import Path
from jarvis_personal_runtime.config import load_runtime_config
from jarvis_personal_runtime.web_tools import WebTools

base = Path("/home/samik/Projects/Jarvis")
loaded = load_runtime_config(
    base / "other/runtime", config_path=base / "other/config/jarvis.toml"
)


async def main():
    tools = WebTools(
        loaded.config.web_search,
        runtime_dir=base / "other/runtime/data/web",
        max_output_chars=65536,
    )
    try:
        result = json.loads(
            await tools.execute(
                "read_web_pages",
                {
                    "urls": ["https://www.iana.org/help/example-domains"],
                    "depth": "quick",
                },
            )
        )
        print(
            json.dumps(
                {
                    "page_outcome": result["outcome"],
                    "pages": [
                        {
                            key: value
                            for key, value in page.items()
                            if key in ("outcome", "url", "title", "reason")
                        }
                        for page in result["pages"]
                    ],
                }
            )
        )
        assert result["outcome"] == "ok"
    finally:
        await tools.close_request()


asyncio.run(main())
start = json.loads(
    (base / "other/runtime/web-acceptance/trace-marker.json").read_text()
)["start"]
for line in (base / "other/runtime/data/runtime-trace.jsonl").read_text().splitlines():
    row = json.loads(line)
    if row["timestamp"] < start or row["event"] not in (
        "openwa_send_attempt",
        "openwa_send_result",
        "openwa_send_error",
    ):
        continue
    payload = row["payload"]
    print(
        json.dumps(
            {
                "timestamp": row["timestamp"],
                "event": row["event"],
                **{
                    k: v
                    for k, v in payload.items()
                    if k in ("text", "disposition", "code", "may_have_sent")
                },
            }
        )
    )
