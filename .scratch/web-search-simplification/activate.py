"""Activate the GitHub-verified candidate with unchanged configuration and rollback."""

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

base = Path("/home/samik/Projects/Jarvis")
revision = sys.argv[1]
assert re.fullmatch("[0-9a-f]{40}", revision)
current = base / "code/current"
previous = current.resolve()
assert previous.name == "9520982fd6fc6f780dfe975b4a6bbe61789b7ff7"
release = base / "code/releases" / revision
config = base / "other/config/jarvis.toml"
prompt = base / "other/runtime/SYSTEM.md"
assert (
    hashlib.sha256(config.read_bytes()).hexdigest()
    == "b6f15064ead9fbff6f543c616ad9c047c0be097df550910450a424515e5a76be"
)
assert (
    hashlib.sha256(prompt.read_bytes()).hexdigest()
    == "33ca22c6f7c7b5b162bd29014139b10ee5b192fec3395136d6bac1672306ba0a"
)
original = {p: p.read_bytes() for p in (config, prompt)}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60)


def service_script(path, *args):
    result = run(
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
        str(path),
        *args,
    )
    print(result.stdout, flush=True)


def point(target):
    pending = current.with_name("current.web-simplification-pending")
    assert not pending.exists() and not pending.is_symlink()
    pending.symlink_to(target)
    pending.replace(current)


service_script(base / "code/web-readiness.py")
service_script(base / "code/web-quality-acceptance.py", "health")
service_script(
    release / ".scratch/web-search-simplification/verify-release.py", revision
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
run("systemctl", "stop", "jarvis-personal-runtime")
try:
    assert all(p.read_bytes() == contents for p, contents in original.items())
    point(release)
    run("systemctl", "start", "jarvis-personal-runtime")
    time.sleep(3)
    run("systemctl", "is-active", "--quiet", "jarvis-personal-runtime")
    service_script(base / "code/web-readiness.py")
    service_script(
        release / ".scratch/web-search-simplification/verify-release.py", revision
    )
    assert all(p.read_bytes() == contents for p, contents in original.items())
except BaseException:
    run("systemctl", "stop", "jarvis-personal-runtime")
    point(previous)
    run("systemctl", "start", "jarvis-personal-runtime")
    print("ROLLED BACK to " + str(previous), file=sys.stderr)
    raise
print(
    json.dumps(
        {
            "active_release": str(current.resolve()),
            "rollback_release": str(previous),
            "config_unchanged": True,
            "prompt_unchanged": True,
        }
    )
)
