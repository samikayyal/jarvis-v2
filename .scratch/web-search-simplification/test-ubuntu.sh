set -eu
cd "$1"
uv sync --locked --python /usr/bin/python3.14
export JARVIS_TEST_CHROMIUM_BINARY=/usr/bin/google-chrome
export JARVIS_TEST_CHROMEDRIVER_BINARY=/usr/local/bin/chromedriver
uv run --no-sync pytest tests/personal_runtime -q
uv run --no-sync ruff check src/jarvis_personal_runtime tests/personal_runtime
uv run --no-sync ruff format --check src/jarvis_personal_runtime tests/personal_runtime
uv run --no-sync python -m compileall -q src/jarvis_personal_runtime
