"""Verify deployed source, reader limits, and gateway without printing secrets."""

import hashlib
import json
import tarfile
from pathlib import Path

import httpx

import jarvis_personal_runtime
from jarvis_personal_runtime.config import load_runtime_config

BASE = Path("/home/samik/Projects/Jarvis")
SHA = "39765480870245a87abc6ee5d9d689af5d6f8aee"
candidate = BASE / "code/releases" / SHA
installed = Path(jarvis_personal_runtime.__file__).parent
assert installed.is_relative_to(candidate / ".venv"), installed
verified = 0
with tarfile.open(BASE / "code/full-access-3976548.tar.gz") as archive:
    for member in archive.getmembers():
        if member.isfile() and member.name.startswith("src/jarvis_personal_runtime/"):
            expected = archive.extractfile(member).read()
            relative = member.name.removeprefix("src/jarvis_personal_runtime/")
            assert (installed / relative).read_bytes() == expected, relative
            assert (candidate / member.name).read_bytes() == expected, member.name
            verified += 1
loaded = load_runtime_config(
    BASE / "other/runtime", config_path=BASE / "other/config/jarvis.toml"
)
cfg = loaded.config
limits = {
    key: getattr(cfg.web_reader, key)
    for key in ("max_attempts", "max_actions", "timeout_seconds", "context_tokens")
}
assert limits == {
    "max_attempts": 24,
    "max_actions": 18,
    "timeout_seconds": 120,
    "context_tokens": 128000,
}
print(
    json.dumps(
        {
            "verified_installed_files": verified,
            "reader_limits": limits,
            "reader_model": cfg.web_reader.model,
            "active_release": str((BASE / "code/current").resolve()),
            "config_sha256": hashlib.sha256(
                (BASE / "other/config/jarvis.toml").read_bytes()
            ).hexdigest(),
            "prompt_sha256": hashlib.sha256(
                (BASE / "other/runtime/SYSTEM.md").read_bytes()
            ).hexdigest(),
            "trace_path": str(cfg.trace_path),
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
                "identity_matches": True,
            }
        )
    )
    assert ready.is_success and state == "ready"
