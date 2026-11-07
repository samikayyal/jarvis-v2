#!/bin/sh
# Exact ticket-04 candidate; deliberately never activates the release.
set -eu
base=/home/samik/Projects/Jarvis/code
sha=551ed852b16f17f87e149feae1690da39114fc5b
archive=$base/parallel-readers-551ed85.tar.gz
candidate=$base/releases/$sha
tests=$base/parallel-readers-test-551ed85
echo 'f1f5c0489be344924da8855e206a8cba5a86116e25262e5fc5c11e144fc29213  '"$archive" | sha256sum -c -
if test ! -e "$candidate"; then
  mkdir "$candidate"
  tar -xzf "$archive" -C "$candidate"
fi
cd "$candidate"
test -x .venv/bin/python || uv venv --python /usr/bin/python3.14 .venv
uv pip install --python .venv/bin/python --require-hashes -r deployment/personal-runtime/requirements.lock
uv pip install --python .venv/bin/python --no-deps --no-build-isolation .
# Install OS dependencies as root, and browser revisions in each actual user's
# default cache. The runtime launch keeps Chromium's sandbox enabled.
# Playwright 1.58 predates Ubuntu 26.04. Select its vendor Ubuntu 24.04
# artifact explicitly; actual compatibility is checked below on this host.
sudo -n env PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64 uv run --no-project --python "$candidate/.venv/bin/python" "$candidate/.venv/bin/playwright" install-deps chromium
sudo -n -H -u jarvis-personal-runtime env PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64 uv run --no-project --python "$candidate/.venv/bin/python" "$candidate/.venv/bin/playwright" install chromium
sudo -n apparmor_parser -Q "$base/jarvis-playwright-04.apparmor"
sudo -n install -o root -g root -m 0644 "$base/jarvis-playwright-04.apparmor" /etc/apparmor.d/jarvis-playwright-04
sudo -n apparmor_parser -r /etc/apparmor.d/jarvis-playwright-04
if test ! -e "$tests"; then
  mkdir "$tests"
  tar -xzf "$archive" -C "$tests"
fi
cd "$tests"
uv sync --locked --python /usr/bin/python3.14
export PLAYWRIGHT_BROWSERS_PATH=/home/samik/Projects/Jarvis/other/runtime/.cache/ms-playwright
export JARVIS_TEST_CHROMIUM_BINARY=/usr/bin/google-chrome
export JARVIS_TEST_CHROMEDRIVER_BINARY=/usr/local/bin/chromedriver
uv run --no-sync ty check src/jarvis_personal_runtime/playwright_reader.py src/jarvis_personal_runtime/playwright_browser.py src/jarvis_personal_runtime/web_tools.py src/jarvis_personal_runtime/page_document.py
uv run --no-sync ruff check src tests deployment/personal-runtime/verify-web.py
uv run --no-sync ruff format --check src tests deployment/personal-runtime/verify-web.py
uv run --no-sync pytest tests/personal_runtime/test_config.py tests/personal_runtime/test_playwright_browser.py tests/personal_runtime/test_concurrent_readers.py tests/personal_runtime/test_reader_integration.py -q
sudo -n -H -u jarvis-personal-runtime "$candidate/.venv/bin/jarvis-personal-runtime" --root /home/samik/Projects/Jarvis/other/runtime --config /home/samik/Projects/Jarvis/other/config/jarvis.toml --check
echo 'Inactive candidate provisioned; full suite and installed-package probes remain.'
