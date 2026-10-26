set -eu
base=/home/samik/Projects/Jarvis
cd /tmp/jarvis-web-deploy-20261018
test "$(git rev-parse HEAD)" = e69864c083a706ec7f4e8d5bfc8cb0cda46ae17e
git diff --check
git add src/jarvis_personal_runtime/web_search.py tests/personal_runtime/test_web_search.py
git commit -m 'Recognize localized empty Google results without hiding source headings'
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
git diff HEAD^ HEAD --stat
sha256sum "$base/code/current/src/jarvis_personal_runtime/vault_git.py" "$release/src/jarvis_personal_runtime/vault_git.py"
printf 'STAGED %s\n' "$revision"
