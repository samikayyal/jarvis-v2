"""Activate one verified web release, preserving config and release rollback."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib

base = Path("/home/samik/Projects/Jarvis")
revision = sys.argv[1]
assert re.fullmatch("[0-9a-f]{40}", revision)
current = base / "code/current"
previous = current.resolve()
assert previous.name == "ff501df0b58c9e38eaa631f17b1e4b9be62a8bdf"
release = base / "code/releases" / revision
config = (base / "other/config/jarvis.toml").resolve()
original = config.read_bytes()
old = tomllib.loads(original.decode())
assert old["web_search"]["max_total_evidence_chars"] == 32000
updated, count = re.subn(
    rb"(?m)^(max_total_evidence_chars\s*=\s*)32000(\s*(?:#.*)?)$",
    rb"\g<1>48000\g<2>",
    original,
)
assert count == 1
expected = tomllib.loads(updated.decode())
expected["web_search"]["max_total_evidence_chars"] = 32000
assert expected == old
prompt = base / "other/runtime/SYSTEM.md"
prompt_hash = hashlib.sha256(prompt.read_bytes()).hexdigest()
assert prompt_hash == "33ca22c6f7c7b5b162bd29014139b10ee5b192fec3395136d6bac1672306ba0a"
backup = (
    base
    / "other/backups"
    / ("web-quality-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
)
backup.mkdir(mode=0o700)
shutil.copy2(config, backup / "jarvis.toml")
(backup / "rollback.json").write_text(
    json.dumps(
        {"release": str(previous), "config": str(config), "prompt_sha256": prompt_hash},
        indent=2,
    )
)
candidate_config = backup / "candidate.toml"
shutil.copy2(config, candidate_config)
candidate_config.write_bytes(updated)
os.chown(candidate_config, config.stat().st_uid, config.stat().st_gid)
# The check runs as the service account, so stage the non-secret config alongside
# its existing config, retaining mode and ownership, before touching the live file.
check_config = config.with_name("jarvis.web-quality-candidate.toml")
assert not check_config.exists()
shutil.copy2(candidate_config, check_config)
os.chown(check_config, config.stat().st_uid, config.stat().st_gid)


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=60)


def validate(target, path):
    run(
        "sudo",
        "-n",
        "-u",
        "jarvis-personal-runtime",
        str(target / ".venv/bin/jarvis-personal-runtime"),
        "--root",
        str(base / "other/runtime"),
        "--config",
        str(path),
        "--check",
    )


def point(target):
    pending = current.with_name("current.web-quality-pending")
    assert not pending.exists() and not pending.is_symlink()
    pending.symlink_to(target)
    pending.replace(current)


validate(release, check_config)
run("systemctl", "stop", "jarvis-personal-runtime")
try:
    assert config.read_bytes() == original
    config.write_bytes(updated)
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
        "python",
        str(base / "code/web-readiness.py"),
    )
    contract = run(
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
        str(base / "code/web-quality-acceptance.py"),
        "contract",
    )
    assert hashlib.sha256(prompt.read_bytes()).hexdigest() == prompt_hash
    print(ready.stdout)
    print(contract.stdout)
except BaseException:
    run("systemctl", "stop", "jarvis-personal-runtime")
    config.write_bytes(original)
    point(previous)
    validate(previous, config)
    run("systemctl", "start", "jarvis-personal-runtime")
    print("ROLLED BACK release and config", file=sys.stderr)
    raise
finally:
    check_config.unlink(missing_ok=True)
report = {
    "active_release": str(current.resolve()),
    "rollback_release": str(previous),
    "backup": str(backup),
    "evidence_chars": 48000,
    "prompt_unchanged": True,
    "config_sha256": hashlib.sha256(updated).hexdigest(),
}
(backup / "activation.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
