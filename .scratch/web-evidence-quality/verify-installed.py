import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

base = Path("/home/samik/Projects/Jarvis")
release = (base / "code/current").resolve()
wheel = base / "code/web-quality-candidate-bcQpYy/dist/jarvis_v2-0.1.0-py3-none-any.whl"
package = (
    Path(importlib.util.find_spec("jarvis_personal_runtime").origin).resolve().parent
)
assert package.is_relative_to(release / ".venv")
verified = 0
with zipfile.ZipFile(wheel) as archive:
    for name in archive.namelist():
        if name.startswith("jarvis_personal_runtime/") and not name.endswith("/"):
            data = archive.read(name)
            assert (package.parent / name).read_bytes() == data, name
            assert (release / "src" / name).read_bytes() == data, name
            verified += 1
print(
    json.dumps(
        {
            "active_release": str(release),
            "installed_package": str(package),
            "verified_packaged_files": verified,
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        }
    )
)
