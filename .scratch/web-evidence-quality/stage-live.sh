set -eu
base=/home/samik/Projects/Jarvis
candidate="$base/code/web-quality-candidate-bcQpYy"
current=$(readlink -f "$base/code/current")
test "$(basename "$current")" = ff501df0b58c9e38eaa631f17b1e4b9be62a8bdf
cd "$candidate"
git apply --check --ignore-whitespace /tmp/jarvis-web-quality-QNRuPi/deploy-web.patch
git apply --ignore-whitespace /tmp/jarvis-web-quality-QNRuPi/deploy-web.patch
tar -xzf /tmp/jarvis-web-quality-QNRuPi/deploy-additions.tar.gz
git diff --check
git add deployment/personal-runtime/README.md deployment/personal-runtime/jarvis.toml.example src/jarvis_personal_runtime/config.py src/jarvis_personal_runtime/skills/web/SKILL.md src/jarvis_personal_runtime/web_browser.py src/jarvis_personal_runtime/web_tools.py src/jarvis_personal_runtime/web_worker.py src/jarvis_personal_runtime/web_evidence.py tests/personal_runtime/test_package.py tests/personal_runtime/test_web_browser.py tests/personal_runtime/test_web_installed_browser.py tests/personal_runtime/test_web_responses.py tests/personal_runtime/test_web_tools.py tests/personal_runtime/test_web_evidence_quality.py
git commit -qm 'Improve bounded web evidence metadata and rendered follow-up reads'
printf '%s\n' "$candidate" > /tmp/jarvis-web-quality-QNRuPi/deployment-candidate-path
printf 'CANDIDATE %s\n' "$candidate"
git show --stat --oneline HEAD
uv sync --locked --python /usr/bin/python3.14
uv run --no-sync ruff check src/jarvis_personal_runtime tests/personal_runtime
uv run --no-sync ruff format --check src/jarvis_personal_runtime tests/personal_runtime
uv run --no-sync python -m compileall -q src/jarvis_personal_runtime
JARVIS_TEST_CHROMIUM_BINARY=/usr/bin/google-chrome JARVIS_TEST_CHROMEDRIVER_BINARY=/usr/local/bin/chromedriver uv run --no-sync pytest tests/personal_runtime -q --tb=short
