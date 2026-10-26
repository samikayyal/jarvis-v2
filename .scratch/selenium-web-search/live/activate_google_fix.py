"""Promote the web fix with unchanged live config and automatic release rollback."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

base = Path("/home/samik/Projects/Jarvis")
revision = sys.argv[1]
assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)
current = base / "code/current"
previous = current.resolve()
assert previous.name == "25ed2fb6286b8c9aaaa15c513f0e9d3ee42b0b93"
release = base / "code/releases" / revision
config = (base / "other/config/jarvis.toml").resolve()


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=45)


def validate(target):
    run(
        "sudo",
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
    pending = current.with_name("current.google-pending")
    assert not pending.exists() and not pending.is_symlink()
    pending.symlink_to(target)
    pending.replace(current)


validate(release)
run("systemctl", "stop", "jarvis-personal-runtime")
original = config.read_bytes()
try:
    point(release)
    run("systemctl", "start", "jarvis-personal-runtime")
    time.sleep(3)
    run("systemctl", "is-active", "--quiet", "jarvis-personal-runtime")
    ready = run(
        "sudo",
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
    assert config.read_bytes() == original
    print(ready.stdout)
except BaseException:
    run("systemctl", "stop", "jarvis-personal-runtime")
    point(previous)
    validate(previous)
    run("systemctl", "start", "jarvis-personal-runtime")
    print("ROLLED BACK", file=sys.stderr)
    raise
print(
    json.dumps(
        {
            "active_release": str(current.resolve()),
            "rollback_release": str(previous),
            "config_unchanged": True,
            "config_sha256": hashlib.sha256(original).hexdigest(),
        }
    )
)
