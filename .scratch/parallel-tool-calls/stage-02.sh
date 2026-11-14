#!/bin/sh
# Install and test the exact GitHub-verified archive without activating it.
set -eu
base=/home/samik/Projects/Jarvis/code
sha=fe70273c5551b2214443d1812365bfa160bc86aa
archive=$base/parallel-tools-$sha.tar.gz
candidate=$base/releases/$sha
tests=$base/parallel-tools-tests-$sha
cd "$base"
sha256sum -c "parallel-tools-$sha.sha256"
test ! -e "$candidate"
mkdir "$candidate" "$tests"
tar -xzf "$archive" -C "$candidate"
tar -xzf "$archive" -C "$tests"
cd "$candidate"
uv venv --python /usr/bin/python3.14 .venv
uv pip install --python .venv/bin/python --require-hashes -r deployment/personal-runtime/requirements.lock
uv pip install --python .venv/bin/python --no-deps --no-build-isolation .
sudo -n -H -u jarvis-personal-runtime "$candidate/.venv/bin/jarvis-personal-runtime" --root /home/samik/Projects/Jarvis/other/runtime --config /home/samik/Projects/Jarvis/other/config/jarvis.toml --check
sudo -n -H -u jarvis-personal-runtime uv run --no-project --python "$candidate/.venv/bin/python" "$base/verify-04.py" --sha "$sha" --archive "$archive"
cd "$tests"
uv sync --locked --python /usr/bin/python3.14
export PLAYWRIGHT_BROWSERS_PATH=/home/samik/Projects/Jarvis/other/runtime/.cache/ms-playwright
export JARVIS_TEST_CHROMIUM_BINARY=/usr/bin/google-chrome
export JARVIS_TEST_CHROMEDRIVER_BINARY=/usr/local/bin/chromedriver
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync python -m compileall -q src/jarvis_personal_runtime
# Existing type diagnostics are retained separately, not mistaken for a pass.
set +e
uv run --no-sync ty check src/jarvis_personal_runtime > "$base/parallel-tools-typecheck-02.log" 2>&1
echo "$?" > "$base/parallel-tools-typecheck-02.exit"
set -e
uv run --no-sync pytest tests/personal_runtime/test_parallel_tools.py -q
set +e
uv run --no-sync pytest -q --tb=short > "$base/parallel-tools-full-suite-02.log" 2>&1
result=$?
echo "$result" > "$base/parallel-tools-full-suite-02.exit"
tail -n 12 "$base/parallel-tools-full-suite-02.log"
exit "$result"
