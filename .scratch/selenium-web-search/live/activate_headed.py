"""Activate a verified release and headed mode, rolling both back on failure."""

import hashlib
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

base = Path("/home/samik/Projects/Jarvis")
revision = sys.argv[1]
assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)
current = base / "code/current"
previous = current.resolve()
assert previous.name == "630811c7ff65868049fd0b1c1c9e05e91daabe93"
release = base / "code/releases" / revision
assert (release / ".venv/bin/jarvis-personal-runtime").is_file()
config = (base / "other/config/jarvis.toml").resolve()
original = config.read_bytes()
before = tomllib.loads(original.decode())
assert "browser_mode" not in before["web_search"]
candidate = original.replace(
    b"[web_search]", b'[web_search]\nbrowser_mode = "headed"', 1
)
after = tomllib.loads(candidate.decode())
assert after["web_search"].pop("browser_mode") == "headed"
assert after == before
backup = base / "other/config" / "jarvis.before-headed.toml"
assert not backup.exists()
backup.write_bytes(original)
backup.chmod(0o600)


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=45)


def validate(target):
    run(
        "sudo",
        "-n",
        "-u",
        "jarvis-personal-runtime",
        str(target / ".venv/bin/jarvis-personal-runtime"),
        "--root",
        str(base / "other/runtime"),
        "--config",
        str(config),
        "--check",
    )


def point(target):
    pending = current.with_name("current.headed-pending")
    assert not pending.exists() and not pending.is_symlink()
    pending.symlink_to(target)
    pending.replace(current)


validate(release)
run("systemctl", "stop", "jarvis-personal-runtime")
try:
    assert config.read_bytes() == original
    config.write_bytes(candidate)
    validate(release)
    point(release)
    run("systemctl", "start", "jarvis-personal-runtime")
    time.sleep(3)
    run("systemctl", "is-active", "--quiet", "jarvis-personal-runtime")
    ready = run(
        "sudo",
        "-n",
        "-u",
        "jarvis-personal-runtime",
        "/usr/local/bin/uv",
        "run",
        "--no-project",
        "--python",
        str(release / ".venv/bin/python"),
        str(base / "code/web-readiness.py"),
    )
    print(ready.stdout)
    assert config.read_bytes() == candidate
except BaseException:
    run("systemctl", "stop", "jarvis-personal-runtime")
    config.write_bytes(original)
    point(previous)
    validate(previous)
    run("systemctl", "start", "jarvis-personal-runtime")
    raise
print(
    json.dumps(
        {
            "active_release": str(current.resolve()),
            "rollback_release": str(previous),
            "config_backup": str(backup),
            "config_sha256": hashlib.sha256(candidate).hexdigest(),
            "only_config_change": "web_search.browser_mode=headed",
        }
    )
)
