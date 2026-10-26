set -eu
base=/home/samik/Projects/Jarvis
cd "$base/code/selenium-web-live"
uv run --no-sync python -c 'from pathlib import Path; paths=[Path("deployment/personal-runtime/README.md"),Path("tests/personal_runtime/test_package.py"),Path("tests/personal_runtime/test_web_installed_browser.py")]; [(p.write_bytes(p.read_bytes().replace(b"\r\n",b"\n"))) for p in paths]'
git diff --check
uv run --no-sync ruff check src/jarvis_personal_runtime tests/personal_runtime
uv run --no-sync ruff format --check src/jarvis_personal_runtime tests/personal_runtime
uv run --no-sync python -m compileall -q src/jarvis_personal_runtime
git add deployment/personal-runtime/README.md deployment/personal-runtime/jarvis.toml.example src/jarvis_personal_runtime/config.py src/jarvis_personal_runtime/web_browser.py src/jarvis_personal_runtime/web_worker.py src/jarvis_personal_runtime/web_search.py src/jarvis_personal_runtime/web_tools.py tests/personal_runtime/test_config.py tests/personal_runtime/test_package.py tests/personal_runtime/test_web_browser.py tests/personal_runtime/test_web_search.py tests/personal_runtime/test_web_installed_browser.py
git commit -m 'Support isolated headed search and opaque Google result links'
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
printf 'STAGED %s\n' "$revision"
