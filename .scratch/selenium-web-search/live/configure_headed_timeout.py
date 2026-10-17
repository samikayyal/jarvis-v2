"""Increase the live browser operation allowance, with validation and rollback."""

import subprocess
import tomllib
from pathlib import Path

base = Path("/home/samik/Projects/Jarvis")
config = (base / "other/config/jarvis.toml").resolve()
original = config.read_bytes()
before = tomllib.loads(original.decode())
assert before["web_search"]["browser_mode"] == "headed"
assert before["web_search"]["operation_timeout_seconds"] == 20
candidate = original.replace(
    b"operation_timeout_seconds = 20", b"operation_timeout_seconds = 60", 1
)
after = tomllib.loads(candidate.decode())
assert after["web_search"].pop("operation_timeout_seconds") == 60
assert before["web_search"].pop("operation_timeout_seconds") == 20
assert after == before
backup = base / "other/config/jarvis.before-headed-timeout.toml"
assert not backup.exists()
backup.write_bytes(original)
backup.chmod(0o600)


def run(*args):
    subprocess.run(args, check=True, timeout=60)


run("systemctl", "stop", "jarvis-personal-runtime")
try:
    config.write_bytes(candidate)
    run(
        "sudo",
        "-n",
        "-u",
        "jarvis-personal-runtime",
        str(base / "code/current/.venv/bin/jarvis-personal-runtime"),
        "--root",
        str(base / "other/runtime"),
        "--config",
        str(config),
        "--check",
    )
    run("systemctl", "start", "jarvis-personal-runtime")
    run("systemctl", "is-active", "--quiet", "jarvis-personal-runtime")
except BaseException:
    config.write_bytes(original)
    run("systemctl", "restart", "jarvis-personal-runtime")
    raise
print("Only configuration change: web_search.operation_timeout_seconds=60")
