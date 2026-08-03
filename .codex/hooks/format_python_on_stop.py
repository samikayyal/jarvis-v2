import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    payload = read_payload()
    cwd = Path(payload.get("cwd") or ".").resolve()
    root = git_root(cwd)
    if root is None:
        return emit_success("Not inside a git repository; Python format hook skipped.")

    result = subprocess.run(
        ["uvx", "ruff", "format", "."],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "uvx ruff format . failed"
        return emit_success(message)
    return emit_success("Ran uvx ruff format .")


def read_payload() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def git_root(cwd: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def emit_success(message: str | None = None) -> int:
    output = {"continue": True}
    if message:
        output["systemMessage"] = message
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
