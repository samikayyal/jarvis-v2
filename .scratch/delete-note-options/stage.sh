set -eu
base=/home/samik/Projects/Jarvis
cd "$base/code/delete-note-options"
sed -i 's/\r$//' src/jarvis_personal_runtime/vault_git.py src/jarvis_personal_runtime/skills/obsidian/SKILL.md tests/personal_runtime/test_vault_delete.py
git diff --check
git diff --stat
uv sync --python /usr/bin/python3.14
uv run --no-sync ruff check src/jarvis_personal_runtime/vault_git.py tests/personal_runtime/test_vault_delete.py
uv run --no-sync ruff format --check src/jarvis_personal_runtime/vault_git.py tests/personal_runtime/test_vault_delete.py
uv run --no-sync pytest -q
uv run --no-sync python -m compileall -q src/jarvis_personal_runtime
git add src/jarvis_personal_runtime/vault_git.py src/jarvis_personal_runtime/skills/obsidian/SKILL.md tests/personal_runtime/test_vault_delete.py
git commit -m 'Make delete-note approval options explicitly one-time or reject'
revision=$(git rev-parse HEAD)
release="$base/code/releases/$revision"
test ! -e "$release"
mkdir "$release"
git archive HEAD | tar -x -C "$release"
uv build --wheel
uv venv --python /usr/bin/python3.14 "$release/.venv"
uv pip install --python "$release/.venv/bin/python" --require-hashes -r "$release/deployment/personal-runtime/requirements.lock"
uv pip install --python "$release/.venv/bin/python" --no-deps dist/jarvis_v2-0.1.0-py3-none-any.whl
sudo -n -u jarvis-personal-runtime "$release/.venv/bin/jarvis-personal-runtime" --root "$base/other/runtime" --config "$base/other/config/jarvis.toml" --check
printf 'Staged candidate: %s\n' "$release"
