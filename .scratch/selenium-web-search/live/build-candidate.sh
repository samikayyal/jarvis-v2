set -eu
base=/home/samik/Projects/Jarvis
cd "$base/code/selenium-web-live"
uv sync --locked --python /usr/bin/python3.14
uv run --no-sync ruff check src/jarvis_personal_runtime tests/personal_runtime deployment/personal-runtime/verify-web.py
uv run --no-sync ruff format --check src/jarvis_personal_runtime tests/personal_runtime deployment/personal-runtime/verify-web.py
JARVIS_TEST_CHROMIUM_BINARY=/usr/bin/google-chrome-stable JARVIS_TEST_CHROMEDRIVER_BINARY=/usr/local/bin/chromedriver uv run --no-sync pytest -q
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
