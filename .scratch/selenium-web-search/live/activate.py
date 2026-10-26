"""One-off activation for the observed relocated native deployment."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib

base = Path("/home/samik/Projects/Jarvis")
revision = sys.argv[1]
assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)
release = base / "code/releases" / revision
current = base / "code/current"
previous = current.resolve()
assert previous.name == "f2d882e90daa5cf04647a8f54ab784a0de8b2e88"
config = (base / "other/config/jarvis.toml").resolve()
assert config == base / "other/config/personal-runtime/jarvis.toml"
backup = base / "other/web-activation-backup" / revision
backup.mkdir(parents=True, mode=0o700)
os.chmod(backup.parent, 0o700)
check = [
    str(release / ".venv/bin/jarvis-personal-runtime"),
    "--root",
    str(base / "other/runtime"),
    "--config",
    str(config),
    "--check",
]


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True)


def point(target):
    temporary = current.with_name("current.web-pending")
    assert not temporary.exists() and not temporary.is_symlink()
    temporary.symlink_to(target)
    temporary.replace(current)


run("systemctl", "stop", "jarvis-personal-runtime")
original = config.read_bytes()
metadata = config.stat()
shutil.copy2(config, backup / "jarvis.toml")
os.chmod(backup / "jarvis.toml", 0o600)
(backup / "previous-release.txt").write_text(str(previous))
try:
    prior = tomllib.loads(original.decode())
    assert "web_search" not in prior
    addition = """
[web_search]
enabled = true
operation_timeout_seconds = 20
results_per_query = 5
max_chars_per_page = 8000
max_total_evidence_chars = 32000
[web_search.quick]
max_queries = 1
max_pages = 2
[web_search.moderate]
max_queries = 3
max_pages = 5
"""
    updated = original.rstrip() + b"\n" + addition.encode()
    parsed = tomllib.loads(updated.decode())
    assert {k: v for k, v in parsed.items() if k != "web_search"} == prior
    temporary = config.with_suffix(".web-pending")
    assert not temporary.exists()
    temporary.write_bytes(updated)
    os.chown(temporary, metadata.st_uid, metadata.st_gid)
    os.chmod(temporary, metadata.st_mode & 0o777)
    temporary.replace(config)
    run("sudo", "-u", "jarvis-personal-runtime", *check)
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
    print(ready.stdout)
except BaseException:
    run("systemctl", "stop", "jarvis-personal-runtime")
    config.write_bytes(original)
    os.chown(config, metadata.st_uid, metadata.st_gid)
    os.chmod(config, metadata.st_mode & 0o777)
    if current.resolve() != previous:
        point(previous)
    run(
        "sudo",
        "-u",
        "jarvis-personal-runtime",
        str(previous / ".venv/bin/jarvis-personal-runtime"),
        *check[1:],
    )
    run("systemctl", "start", "jarvis-personal-runtime")
    print("ROLLED BACK", file=sys.stderr)
    raise
print(
    json.dumps(
        {
            "active_release": str(current.resolve()),
            "rollback_backup": str(backup),
            "saved_permissions_preserved": True,
            "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        }
    )
)
