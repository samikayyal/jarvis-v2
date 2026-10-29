set -eu
revision="$1"
base=/home/samik/Projects/Jarvis
source="$base/code/web-simplification-source-$revision"
release="$base/code/releases/$revision"
test ! -e "$source"
test ! -e "$release"
mkdir "$source"
git -C "$source" init -q
git -C "$source" fetch /tmp/jarvis-web-simplification.bundle HEAD
git -C "$source" checkout --detach FETCH_HEAD
test "$(git -C "$source" rev-parse HEAD)" = "$revision"
cd "$source"
uv build --wheel --python /usr/bin/python3.14
mkdir "$release"
git archive HEAD | tar -x -C "$release"
uv venv --python /usr/bin/python3.14 "$release/.venv"
uv pip install --python "$release/.venv/bin/python" --require-hashes -r "$release/deployment/personal-runtime/requirements.lock"
uv pip install --python "$release/.venv/bin/python" --no-deps dist/jarvis_v2-0.1.0-py3-none-any.whl
sudo -n -u jarvis-personal-runtime "$release/.venv/bin/jarvis-personal-runtime" --root "$base/other/runtime" --config "$base/other/config/jarvis.toml" --check
sudo -n -u jarvis-personal-runtime uv run --no-project --python "$release/.venv/bin/python" python "$release/.scratch/web-search-simplification/verify-release.py" "$revision"
printf 'STAGED %s\n' "$release"
