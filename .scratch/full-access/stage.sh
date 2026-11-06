#!/bin/sh
set -eu
base=/home/samik/Projects/Jarvis/code
sha=39765480870245a87abc6ee5d9d689af5d6f8aee
candidate=$base/releases/$sha
test ! -e "$candidate"
mkdir "$candidate"
tar -xzf "$base/full-access-3976548.tar.gz" -C "$candidate"
cd "$candidate"
uv venv --python /usr/bin/python3.14 .venv
uv pip install --python .venv/bin/python --require-hashes -r deployment/personal-runtime/requirements.lock
uv pip install --python .venv/bin/python --no-deps --no-build-isolation .
testdir=$base/full-access-test-3976548
test ! -e "$testdir"
mkdir "$testdir"
tar -xzf "$base/full-access-3976548.tar.gz" -C "$testdir"
cd "$testdir"
uv sync --locked --python /usr/bin/python3.14
export JARVIS_TEST_CHROMIUM_BINARY=/usr/bin/google-chrome
export JARVIS_TEST_CHROMEDRIVER_BINARY=/usr/local/bin/chromedriver
uv run --no-sync pytest tests/personal_runtime -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
sudo -n -u jarvis-personal-runtime "$candidate/.venv/bin/jarvis-personal-runtime" --root /home/samik/Projects/Jarvis/other/runtime --config /home/samik/Projects/Jarvis/other/config/jarvis.toml --check
