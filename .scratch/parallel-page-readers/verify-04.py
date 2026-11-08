"""Read-only installed-candidate and exact gateway-session checks; no secrets output."""

import argparse
import hashlib
import json
import tarfile
from pathlib import Path

import httpx

import jarvis_personal_runtime
from jarvis_personal_runtime.config import load_runtime_config

BASE = Path("/home/samik/Projects/Jarvis")
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--sha", default="551ed852b16f17f87e149feae1690da39114fc5b")
parser.add_argument(
    "--archive", type=Path, default=BASE / "code/parallel-readers-551ed85.tar.gz"
)
parser.add_argument("--config", type=Path, default=BASE / "other/config/jarvis.toml")
parser.add_argument("--expected-active", type=Path)
args = parser.parse_args()
SHA = args.sha
assert len(SHA) == 40 and all(c in "0123456789abcdef" for c in SHA)
candidate = BASE / "code/releases" / SHA
installed = Path(jarvis_personal_runtime.__file__).parent.resolve()
assert installed.is_relative_to(candidate / ".venv"), installed
verified = 0
with tarfile.open(args.archive) as archive:
    for member in archive.getmembers():
        if not member.isfile():
            continue
        archived_file = archive.extractfile(member)
        assert archived_file is not None, member.name
        data = archived_file.read()
        assert (candidate / member.name).read_bytes() == data, member.name
        if member.name.startswith("src/jarvis_personal_runtime/"):
            relative = member.name.removeprefix("src/jarvis_personal_runtime/")
            assert (installed / relative).read_bytes() == data, relative
            verified += 1
assert not (installed / "page_reader.py").exists()
loaded = load_runtime_config(BASE / "other/runtime", config_path=args.config)
cfg = loaded.config
assert cfg.web_reader is not None
assert cfg.openwa_api_base_url is not None
if args.expected_active:
    assert (BASE / "code/current").resolve() == args.expected_active.resolve()
limits = {
    key: getattr(cfg.web_reader, key)
    for key in (
        "max_attempts",
        "timeout_seconds",
        "context_tokens",
        "output_tokens",
        "max_active_readers",
        "max_browser_contexts",
        "max_readers",
    )
}
assert cfg.web_reader.model == "gpt-6-luna"
assert limits["max_active_readers"] == limits["max_browser_contexts"] == 3
assert limits["max_readers"] == 8
print(
    json.dumps(
        {
            "candidate_sha": SHA,
            "verified_installed_files": verified,
            "all_archived_source_files_match": True,
            "legacy_reader_absent": True,
            "reader_limits": limits,
            "reader_model": cfg.web_reader.model,
            "reader_reasoning": cfg.web_reader.reasoning_effort,
            "main_model": cfg.model,
            "main_reasoning": cfg.reasoning_effort,
            "active_release": str((BASE / "code/current").resolve()),
            "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
            "prompt_sha256": hashlib.sha256(
                (BASE / "other/runtime/SYSTEM.md").read_bytes()
            ).hexdigest(),
        }
    )
)
with httpx.Client(
    timeout=15, headers={"X-API-Key": loaded.secrets.openwa_api_key}
) as client:
    ready = client.get(cfg.openwa_api_base_url.rstrip("/") + "/health/ready")
    response = client.get(cfg.openwa_api_base_url.rstrip("/") + "/sessions")
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("sessions", payload))
    if isinstance(payload, dict):
        payload = payload.get("sessions", [payload])
    matches = [
        s
        for s in payload
        if s.get("id", s.get("sessionId")) == cfg.openwa_internal_session_id
    ]
    assert len(matches) == 1 and matches[0].get("name") == cfg.openwa_named_session
    state = matches[0].get("status", matches[0].get("state"))
    print(
        json.dumps(
            {
                "gateway_ready_http": ready.status_code,
                "session_state": state,
                "configured_named_session_matches": True,
            }
        )
    )
    assert ready.is_success and state == "ready"
