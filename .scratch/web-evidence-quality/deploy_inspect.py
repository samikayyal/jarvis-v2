import hashlib
import json
from pathlib import Path
import zipfile

base = Path("/home/samik/Projects/Jarvis")
current = (base / "code/current").resolve()
wheel = Path("/tmp/jarvis-web-quality-QNRuPi/jarvis_v2-0.1.0-py3-none-any.whl")
differences = []
with zipfile.ZipFile(wheel) as archive:
    for name in archive.namelist():
        if not name.startswith("jarvis_personal_runtime/") or name.endswith("/"):
            continue
        target = current / "src" / name
        data = archive.read(name)
        if not target.exists() or target.read_bytes().replace(
            b"\r\n", b"\n"
        ) != data.replace(b"\r\n", b"\n"):
            differences.append(name)
print(
    json.dumps(
        {
            "active": str(current),
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "changed_package_files": differences,
        },
        indent=2,
    )
)
prompt = (base / "other/runtime/SYSTEM.md").read_text()
print("SYSTEM.md:\n" + prompt)
