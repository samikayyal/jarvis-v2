"""Activate an already GitHub-verified release; roll back source and config on failure."""

import asyncio
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

base = Path("/home/samik/Projects/Jarvis")
revision = sys.argv[1]
assert re.fullmatch("[0-9a-f]{40}", revision)
release = base / "code/releases" / revision
current = base / "code/current"
previous = current.resolve()
assert previous.name == "f8f78112d5e1c98473be588709e1bd86c6d7f412", previous
config = base / "other/config/jarvis.toml"
prompt = base / "other/runtime/SYSTEM.md"
original, original_prompt = config.read_bytes(), prompt.read_bytes()
assert b"[web_reader]" not in original


def run(*args):
    result = subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=60
    )
    print(result.stdout, end="", flush=True)
    return result


def point(target):
    pending = current.with_name("current.page-reader-pending")
    assert not pending.exists() and not pending.is_symlink()
    pending.symlink_to(target)
    pending.replace(current)


def health():
    run(
        "sudo",
        "-n",
        "-u",
        "jarvis-personal-runtime",
        "/usr/local/bin/uv",
        "run",
        "--no-project",
        "--python",
        str(release / ".venv/bin/python"),
        "python",
        str(base / "code/web-readiness.py"),
    )


# Run this script using the candidate environment; every installed file must
# match the archive of the verified commit before the symlink changes.
package = Path(importlib.util.find_spec("jarvis_personal_runtime").origin).parent
assert package.is_relative_to(release / ".venv")
source = release / "src/jarvis_personal_runtime"
for path in source.rglob("*"):
    if path.is_file() and "__pycache__" not in path.parts:
        assert path.read_bytes() == (package / path.relative_to(source)).read_bytes(), (
            path
        )
health()
run("systemctl", "stop", "jarvis-personal-runtime")
try:
    assert config.read_bytes() == original and prompt.read_bytes() == original_prompt
    config.with_name("jarvis.before-page-reader.toml").write_bytes(original)
    config.write_bytes(
        original + b'\n[web_reader]\nmodel = "gpt-6-luna"\nreasoning_effort = "low"\n'
    )
    run(
        "sudo",
        "-n",
        "-u",
        "jarvis-personal-runtime",
        str(release / ".venv/bin/jarvis-personal-runtime"),
        "--root",
        str(base / "other/runtime"),
        "--config",
        str(config),
        "--check",
    )
    from jarvis_personal_runtime.config import load_runtime_config
    from jarvis_personal_runtime.responses import build_direct_responses_runner

    loaded = load_runtime_config(base / "other/runtime", config_path=config)
    runner = build_direct_responses_runner("unused-contract-check", loaded.config)
    asyncio.run(runner._tools.execute("load_skill", {"name": "web"}))
    names = {d["name"] for d in runner._tools.definitions}
    assert {"search_web", "read_web_page", "ask_page_reader"} <= names
    assert "read_web_pages" not in names
    point(release)
    run("systemctl", "start", "jarvis-personal-runtime")
    time.sleep(3)
    run("systemctl", "is-active", "--quiet", "jarvis-personal-runtime")
    health()
    assert prompt.read_bytes() == original_prompt
except BaseException:
    run("systemctl", "stop", "jarvis-personal-runtime")
    config.write_bytes(original)
    point(previous)
    run("systemctl", "start", "jarvis-personal-runtime")
    raise
print(
    json.dumps(
        {
            "release": revision,
            "rollback": previous.name,
            "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "prompt_unchanged": True,
        }
    )
)
