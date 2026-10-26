import os
from pathlib import Path
import subprocess
import sys
from jarvis_personal_runtime.web_browser import _minimal_environment

result = subprocess.run(
    [sys.executable, "/home/samik/Projects/Jarvis/code/web-startup-probe.py"],
    env=_minimal_environment(),
    timeout=50,
)
raise SystemExit(result.returncode)
