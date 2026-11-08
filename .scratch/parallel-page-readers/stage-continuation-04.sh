#!/bin/sh
# Stage the GitHub-verified corrective source; no activation.
set -eu
base=/home/samik/Projects/Jarvis/code
sha=9c2f7d90b867f63cdd536ac0970916ed8143e796
archive=$base/readers-continuation.tar.gz
candidate=$base/releases/$sha
tests=$base/readers-tests-$sha
echo 'd605b3eae7bf43f0066f492a42179115774fcfab15f57cc9cb14da07e298a40a  '"$archive" | sha256sum -c -
if test ! -e "$candidate"; then
  mkdir "$candidate"
  tar -xzf "$archive" -C "$candidate"
fi
cd "$candidate"
test -x .venv/bin/python || uv venv --python /usr/bin/python3.14 .venv
uv pip install --python .venv/bin/python --require-hashes -r deployment/personal-runtime/requirements.lock
uv pip install --python .venv/bin/python --no-deps --no-build-isolation .
if test ! -e "$tests"; then
  mkdir "$tests"
  tar -xzf "$archive" -C "$tests"
fi
cd "$tests"
uv sync --locked --python /usr/bin/python3.14
export PLAYWRIGHT_BROWSERS_PATH=/home/samik/Projects/Jarvis/other/runtime/.cache/ms-playwright
uv run --no-sync ty check src/jarvis_personal_runtime/playwright_browser.py src/jarvis_personal_runtime/playwright_reader.py
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest tests/personal_runtime/test_playwright_browser.py tests/personal_runtime/test_concurrent_readers.py tests/personal_runtime/test_reader_integration.py tests/personal_runtime/test_playwright_reader.py -q
uv run --no-project --python "$candidate/.venv/bin/python" "$base/prepare-config-04.py" "$sha"
config=/home/samik/Projects/Jarvis/other/config/jarvis.reader-fix-$sha.toml
sudo -n -H -u jarvis-personal-runtime "$candidate/.venv/bin/jarvis-personal-runtime" --root /home/samik/Projects/Jarvis/other/runtime --config "$config" --check
sudo -n -H -u jarvis-personal-runtime uv run --no-project --python "$candidate/.venv/bin/python" "$base/verify-04.py" --sha "$sha" --archive "$archive" --config "$config"
