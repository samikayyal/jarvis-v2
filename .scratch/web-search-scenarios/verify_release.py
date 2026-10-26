"""Verify the active artifact and unchanged deployment inputs."""

import hashlib
import json
from pathlib import Path
import subprocess
import jarvis_personal_runtime.web_search as search

base = Path("/home/samik/Projects/Jarvis")
release = (base / "code/current").resolve()
prior = base / "code/releases/e69864c083a706ec7f4e8d5bfc8cb0cda46ae17e"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


assert release.name == "5dfac1eebb68d32f7a0f60ea1b23616bc1a93662"
source = release / "src/jarvis_personal_runtime/web_search.py"
assert digest(source) == digest(Path(search.__file__))
assert digest(release / "src/jarvis_personal_runtime/vault_git.py") == digest(
    prior / "src/jarvis_personal_runtime/vault_git.py"
)
config_hash = digest(base / "other/config/jarvis.toml")
prompt_hash = digest(base / "other/runtime/SYSTEM.md")
assert config_hash == "3c638aae8d216b145b8e41f73502137500c3ae65901270ee826237cf1ce07d4f"
assert prompt_hash == "eb5ab2cd13457ba4a3b0db2a702c2bb9c7c1ce967e1a84e31335c1c684373247"
state = subprocess.run(
    [
        "systemctl",
        "show",
        "jarvis-personal-runtime",
        "-p",
        "ActiveState",
        "-p",
        "SubState",
        "-p",
        "NRestarts",
    ],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
assert (
    "ActiveState=active" in state
    and "SubState=running" in state
    and "NRestarts=0" in state
)
print(
    json.dumps(
        {
            "release": release.name,
            "installed_source_sha256": digest(source),
            "vault_preserved": True,
            "config_sha256": config_hash,
            "prompt_sha256": prompt_hash,
            "service": state,
        }
    )
)
