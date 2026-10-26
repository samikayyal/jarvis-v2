import asyncio
import json
from pathlib import Path
from jarvis_personal_runtime.config import load_runtime_config
from jarvis_personal_runtime.responses import build_direct_responses_runner

base = Path("/home/samik/Projects/Jarvis")
loaded = load_runtime_config(
    base / "other/runtime", config_path=base / "other/config/jarvis.toml"
)
runner = build_direct_responses_runner(
    "unused-no-model-request", loaded.config, config_path=loaded.toml_path
)
tools = runner._tools
assert "web" in tools.instructions
asyncio.run(tools.execute("load_skill", {"name": "web"}))
names = sorted(d["name"] for d in tools.definitions)
assert "search_web" in names and "read_web_pages" in names
print(
    json.dumps(
        {
            "web_enabled": loaded.config.web_search.enabled,
            "loaded_web_tools": names,
            "prompt_has_skill_guidance": "Use the available skills and prepared tools"
            in loaded.system_prompt,
        }
    )
)
