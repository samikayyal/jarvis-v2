set -eu
base=/home/samik/Projects/Jarvis
candidate="$base/code/web-quality-candidate-bcQpYy"
cd "$candidate"
git add tests/personal_runtime/test_package.py
git commit -m 'Normalize package contract test line endings'
git diff --check
uv run --no-sync python -c 'from pathlib import Path; import hashlib,json; base=Path("/home/samik/Projects/Jarvis/code/current/src/jarvis_personal_runtime"); root=Path("src/jarvis_personal_runtime"); allowed={"config.py","web_browser.py","web_evidence.py","web_tools.py","web_worker.py","skills/web/SKILL.md"}; changed={str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts and (not (base/p.relative_to(root)).exists() or p.read_bytes().replace(b"\r\n",b"\n")!=(base/p.relative_to(root)).read_bytes().replace(b"\r\n",b"\n"))}; missing={str(p.relative_to(base)) for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts and not (root/p.relative_to(base)).exists()}; assert changed==allowed,(changed,allowed); assert not missing,missing; print(json.dumps({"changed_runtime_files":sorted(changed),"unrelated_runtime_preserved":True})); assert "For pricing comparisons" in (root/"skills/web/SKILL.md").read_text()'
uv build --wheel --python /usr/bin/python3.14
revision=$(git rev-parse HEAD)
release="$base/code/releases/$revision"
test ! -e "$release"
mkdir "$release"
git archive HEAD | tar -x -C "$release"
uv venv --python /usr/bin/python3.14 "$release/.venv"
uv pip install --python "$release/.venv/bin/python" --require-hashes -r "$release/deployment/personal-runtime/requirements.lock"
uv pip install --python "$release/.venv/bin/python" --no-deps dist/jarvis_v2-0.1.0-py3-none-any.whl
sudo -n -u jarvis-personal-runtime "$release/.venv/bin/jarvis-personal-runtime" --root "$base/other/runtime" --config "$base/other/config/jarvis.toml" --check
cp /tmp/jarvis-web-quality-QNRuPi/live_acceptance.py "$base/code/web-quality-acceptance.py"
sudo -n -u jarvis-personal-runtime uv run --no-project --python "$release/.venv/bin/python" python "$base/code/web-quality-acceptance.py" contract
git bundle create /tmp/jarvis-web-quality-QNRuPi/deployment-source.bundle HEAD
printf '%s\n' "$revision" > /tmp/jarvis-web-quality-QNRuPi/deployment-revision
sha256sum dist/jarvis_v2-0.1.0-py3-none-any.whl
printf 'STAGED %s\n' "$release"
