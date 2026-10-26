import json
from pathlib import Path
import pwd
import subprocess
from jarvis_personal_runtime.config import load_runtime_config

base = Path("/home/samik/Projects/Jarvis")
loaded = load_runtime_config(
    base / "other/runtime", config_path=base / "other/config/jarvis.toml"
)
uid = pwd.getpwnam("jarvis-personal-runtime").pw_uid
processes = []
for directory in Path("/proc").iterdir():
    if not directory.name.isdigit():
        continue
    try:
        if directory.stat().st_uid != uid:
            continue
        command = (
            (directory / "cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode(errors="replace")
        )
        executable = (directory / "comm").read_text().strip()
        if (
            executable in ("chrome", "chromedriver", "chrome_crashpad", "Xvfb")
            or " -m jarvis_personal_runtime.web_worker " in command
        ):
            processes.append({"pid": directory.name, "executable": executable})
    except (OSError, ProcessLookupError):
        pass
web_root = base / "other/runtime/data/web"
profiles = list(web_root.glob(".jarvis-web-*"))
report = {
    "active_release": str((base / "code/current").resolve()),
    "browser_processes": processes,
    "profile_count": len(profiles),
    "config_web_enabled": loaded.config.web_search.enabled,
    "browser_mode": loaded.config.web_search.browser_mode,
}
print(json.dumps(report))
assert not processes and not profiles
