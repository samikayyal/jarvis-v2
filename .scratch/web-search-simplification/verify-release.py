"""Verify the installed candidate, preserved source, and live tool contract."""

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from jarvis_personal_runtime.config import load_runtime_config
from jarvis_personal_runtime.responses import build_direct_responses_runner

base = Path("/home/samik/Projects/Jarvis")
release = base / "code/releases" / sys.argv[1]
package = Path(importlib.util.find_spec("jarvis_personal_runtime").origin).parent
assert package.is_relative_to(release / ".venv"), package
source = release / "src/jarvis_personal_runtime"
files = [p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
for path in files:
    assert path.read_bytes() == (package / path.relative_to(source)).read_bytes(), path
active = (base / "code/current").resolve()
allowed = {"web_tools.py", "web_evidence.py", "web_worker.py", "skills/web/SKILL.md"}
changed = set()
for path in files:
    relative = path.relative_to(source)
    previous = active / "src/jarvis_personal_runtime" / relative
    if path.read_bytes().replace(b"\r\n", b"\n") != previous.read_bytes().replace(
        b"\r\n", b"\n"
    ):
        changed.add(relative.as_posix())
assert changed <= allowed, changed
loaded = load_runtime_config(
    base / "other/runtime", config_path=base / "other/config/jarvis.toml"
)
runner = build_direct_responses_runner(
    "unused-no-model-request", loaded.config, config_path=loaded.toml_path
)
tools = runner._tools
assert "run_terminal" in {d["name"] for d in tools.definitions}
asyncio.run(tools.execute("load_skill", {"name": "web"}))
definition = next(d for d in tools.definitions if d["name"] == "read_web_pages")
props = definition["parameters"]["properties"]
assert props["urls"]["maxItems"] == 5
assert props["render_mode"]["enum"] == ["static", "rendered"]
assert "Public text on a page with a login widget" in tools.instructions
assert "For pricing comparisons" in tools.instructions
prompt = (base / "other/runtime/SYSTEM.md").read_bytes()
assert (
    prompt == (release / "deployment/personal-runtime/SYSTEM.md.example").read_bytes()
)
print(
    json.dumps(
        {
            "release": release.name,
            "verified_package_files": len(files),
            "changed_runtime_files": sorted(changed),
            "max_urls": 5,
            "tool_contract": True,
            "live_prompt_matches_candidate": True,
            "config_sha256": hashlib.sha256(loaded.toml_path.read_bytes()).hexdigest(),
            "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        }
    )
)
