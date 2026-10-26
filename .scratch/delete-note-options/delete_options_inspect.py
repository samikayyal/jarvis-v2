import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

from jarvis_personal_runtime.config import load_runtime_config

base = Path("/home/samik/Projects/Jarvis")
loaded = load_runtime_config(
    base / "other/runtime", config_path=base / "other/config/jarvis.toml"
)
vault = loaded.config.vault_path
settings = loaded.config.vault_git
env = os.environ.copy()
env["GIT_SSH_COMMAND"] = " ".join(
    [
        "ssh",
        "-i",
        shlex.quote(str(settings.ssh_identity_file)),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        shlex.quote("UserKnownHostsFile=" + str(settings.ssh_known_hosts_file)),
    ]
)


def git(*args):
    return subprocess.check_output(
        ["git", "-C", str(vault), *args], env=env, text=True
    ).strip()


note = vault / "Projects/Jarvis/delete-options-test-2026-10-05.md"
print(
    json.dumps(
        {
            "note_exists": note.exists(),
            "note_sha256": hashlib.sha256(note.read_bytes()).hexdigest()
            if note.exists()
            else None,
            "head": git("rev-parse", "HEAD"),
            "remote_head": git(
                "ls-remote", settings.remote, "refs/heads/" + settings.branch
            ).split()[0],
            "clean": not git("status", "--porcelain"),
            "config_sha256": hashlib.sha256(loaded.toml_path.read_bytes()).hexdigest(),
            "prompt_sha256": hashlib.sha256(
                (base / "other/runtime/SYSTEM.md").read_bytes()
            ).hexdigest(),
        }
    )
)
for line in loaded.config.trace_path.read_text().splitlines()[-40:]:
    event = json.loads(line)
    payload = event["payload"]
    safe = {
        k: payload[k]
        for k in ("disposition", "choice", "decision", "tool_name", "name")
        if k in payload
    }
    if event["event"] not in (
        "responses_request",
        "responses_parsed",
        "responses_http_response",
    ):
        print(event["timestamp"], event["event"], json.dumps(safe))
