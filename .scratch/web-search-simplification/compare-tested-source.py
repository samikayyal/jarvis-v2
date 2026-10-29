"""Match the Ubuntu tested source to the GitHub-verified release archive."""

import sys
from pathlib import Path

release = Path(sys.argv[1])
files = [
    path
    for directory in ("src/jarvis_personal_runtime", "tests/personal_runtime")
    for path in Path(directory).rglob("*")
    if path.is_file() and "__pycache__" not in path.parts
]
mismatches = [
    str(path)
    for path in files
    if path.read_bytes().replace(b"\r\n", b"\n")
    != (release / path).read_bytes().replace(b"\r\n", b"\n")
]
assert not mismatches, mismatches
print("Tested source matches verified candidate:", len(files), "files")
