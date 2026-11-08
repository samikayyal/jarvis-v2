"""Stage the current runtime workspace correction without changing live config."""

import hashlib
import shutil
import sys
import tomllib
from pathlib import Path

sha = sys.argv[1]
assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
root = Path("/home/samik/Projects/Jarvis/other/config")
live = root / "jarvis.toml"
backup = root / f"jarvis.pre-reader-fix-{sha}.toml"
staged = root / f"jarvis.reader-fix-{sha}.toml"
original = live.read_text()
old = 'ubuntu_working_directory = "/home/samik/Projects/Jarvis/code/legacy-workspace"'
new = 'ubuntu_working_directory = "/home/samik/Projects/Jarvis/code/current"'
assert original.count(old) == 1
candidate = original.replace(old, new)
before, after = tomllib.loads(original), tomllib.loads(candidate)
assert before["runtime"].pop("ubuntu_working_directory")
assert after["runtime"].pop("ubuntu_working_directory")
assert before == after
if backup.exists():
    assert backup.read_bytes() == live.read_bytes(), "Live config changed since staging"
else:
    shutil.copy2(live, backup)
shutil.copy2(live, staged)
staged.write_text(candidate)
print("Original config SHA256:", hashlib.sha256(live.read_bytes()).hexdigest())
print("Staged config SHA256:", hashlib.sha256(staged.read_bytes()).hexdigest())
