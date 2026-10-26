import json
import hashlib
import tomllib
from pathlib import Path
import httpx
from jarvis_personal_runtime.config import load_runtime_config
from jarvis_personal_runtime.openwa import OpenWASettings

base = Path("/home/samik/Projects/Jarvis")
loaded = load_runtime_config(
    base / "other/runtime", config_path=base / "other/config/jarvis.toml"
)
settings = OpenWASettings.from_loaded_config(loaded)
with httpx.Client(timeout=15, headers={"X-API-Key": settings.api_key}) as client:
    health = client.get(settings.api_base_url.rstrip("/") + "/health/ready")
    response = client.get(settings.api_base_url.rstrip("/") + "/sessions")
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("sessions", payload))
    if isinstance(payload, dict):
        payload = payload.get("sessions", [payload])
    matches = [
        s
        for s in payload
        if s.get("id", s.get("sessionId")) == settings.internal_session_id
    ]
    assert len(matches) == 1 and matches[0].get("name") == settings.named_session
    state = matches[0].get("status", matches[0].get("state"))
    assert health.is_success and state == "ready"
config = tomllib.loads(loaded.toml_path.read_text())
print(
    json.dumps(
        {
            "gateway_ready_http": health.status_code,
            "named_session": state,
            "identity_matches": True,
            "model": loaded.config.model,
            "web_search": config.get("web_search"),
            "prompt_matches_example": loaded.system_prompt.strip()
            == (
                base
                / "code/selenium-web-live/deployment/personal-runtime/SYSTEM.md.example"
            )
            .read_text()
            .strip(),
            "config_sha256": hashlib.sha256(loaded.toml_path.read_bytes()).hexdigest(),
            "prompt_sha256": hashlib.sha256(
                (base / "other/runtime/SYSTEM.md").read_bytes()
            ).hexdigest(),
            "listener": config["runtime"]["listener_host"],
            "trace_path": str(loaded.config.trace_path),
        }
    )
)
