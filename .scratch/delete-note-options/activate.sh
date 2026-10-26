set -eu
base=/home/samik/Projects/Jarvis
candidate="$base/code/releases/f2d882e90daa5cf04647a8f54ab784a0de8b2e88"
previous=$(readlink -f "$base/code/current")
test "$previous" = "$base/code/releases/446fd74145c8fa8bab2812d5dc3c893e1f854188"
sudo -n -u jarvis-personal-runtime "$candidate/.venv/bin/jarvis-personal-runtime" --root "$base/other/runtime" --config "$base/other/config/jarvis.toml" --check
rollback() {
    trap - EXIT
    sudo -n systemctl stop jarvis-personal-runtime || true
    ln -sfn "$previous" "$base/code/current.delete-rollback"
    mv -Tf "$base/code/current.delete-rollback" "$base/code/current"
    sudo -n systemctl start jarvis-personal-runtime
}
trap rollback EXIT
sudo -n systemctl stop jarvis-personal-runtime
ln -sfn "$candidate" "$base/code/current.delete-new"
mv -Tf "$base/code/current.delete-new" "$base/code/current"
sudo -n systemctl start jarvis-personal-runtime
systemctl is-active --quiet jarvis-personal-runtime
test "$(readlink -f "$base/code/current")" = "$candidate"
sudo -n -u jarvis-personal-runtime /usr/local/bin/uv run --no-project --python "$candidate/.venv/bin/python" "$base/code/skills-verify.py"
trap - EXIT
printf 'Activated: %s\nRollback release: %s\n' "$candidate" "$previous"
systemctl show jarvis-personal-runtime -p ActiveState -p SubState -p NRestarts --no-pager
