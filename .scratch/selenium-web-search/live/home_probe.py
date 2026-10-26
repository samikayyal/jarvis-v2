import subprocess
import sys
import tempfile
from jarvis_personal_runtime.web_browser import _minimal_environment

with tempfile.TemporaryDirectory(prefix="jarvis-home-probe-") as folder:
    environment = _minimal_environment()
    environment["HOME"] = folder
    result = subprocess.run(
        [sys.executable, "/home/samik/Projects/Jarvis/code/web-startup-probe.py"],
        env=environment,
        timeout=50,
    )
    raise SystemExit(result.returncode)
