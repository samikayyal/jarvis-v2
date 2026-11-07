#!/bin/sh
# Run only after the local GitHub exact-SHA gate and staged acceptance gates.
set -eu
base=/home/samik/Projects/Jarvis/code
candidate=$base/releases/551ed852b16f17f87e149feae1690da39114fc5b
previous=$base/releases/39765480870245a87abc6ee5d9d689af5d6f8aee
root=/home/samik/Projects/Jarvis/other/runtime
config=/home/samik/Projects/Jarvis/other/config/jarvis.toml
test "$(readlink -f "$base/current")" = "$previous"
test "$(cat "$base/full-suite-04.exit")" = 0
test "$(cat "$base/screenshot-04.exit")" = 0
test "$(sha256sum "$config" | cut -d' ' -f1)" = 9d76db623deef741509923396b36e46c67d5b4ab94ffa8a07a105ebb94f90c59
cmp "$root/SYSTEM.md" "$candidate/deployment/personal-runtime/SYSTEM.md.example"
sudo -n -H -u jarvis-personal-runtime "$candidate/.venv/bin/jarvis-personal-runtime" --root "$root" --config "$config" --check
sudo -n -H -u jarvis-personal-runtime uv run --no-project --python "$candidate/.venv/bin/python" "$base/verify-04.py"
systemd-analyze verify /etc/systemd/system/jarvis-personal-runtime.service
rollback() {
  sudo -n systemctl stop jarvis-personal-runtime
  sudo -n ln -sfn "$previous" "$base/current.parallel-rollback"
  sudo -n mv -Tf "$base/current.parallel-rollback" "$base/current"
  sudo -n systemctl start jarvis-personal-runtime
}
trap 'rollback' HUP INT TERM
sudo -n systemctl stop jarvis-personal-runtime
sudo -n ln -sfn "$candidate" "$base/current.parallel-next"
if ! sudo -n mv -Tf "$base/current.parallel-next" "$base/current"; then rollback; exit 1; fi
if ! sudo -n systemctl start jarvis-personal-runtime; then rollback; exit 1; fi
if ! sudo -n systemctl is-active --quiet jarvis-personal-runtime; then rollback; exit 1; fi
trap - HUP INT TERM
readlink -f "$base/current"
systemctl show jarvis-personal-runtime -p ActiveState -p SubState -p NRestarts -p MainPID
systemctl is-enabled jarvis-personal-runtime
