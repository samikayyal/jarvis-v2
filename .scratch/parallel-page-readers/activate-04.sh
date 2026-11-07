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
verify_health() {
  expected=$1
  # Type=simple reports active before Python imports and listener startup finish.
  # Bound actual listener readiness instead of assuming a three-second startup.
  attempt=0
  until sudo -n systemctl is-active --quiet jarvis-personal-runtime &&
    ss -ltnH | awk '$4 == "172.20.0.1:9011" { found=1 } END { exit !found }'; do
    attempt=$((attempt + 1))
    test "$attempt" -lt 45 || return 1
    sleep 1
  done
  systemctl is-enabled --quiet jarvis-personal-runtime || return 1
  test "$(readlink -f "$base/current")" = "$expected" || return 1
  test "$(systemctl show jarvis-personal-runtime -p NRestarts --value)" = 0 || return 1
  sudo -n -H -u jarvis-personal-runtime "$expected/.venv/bin/jarvis-personal-runtime" --root "$root" --config "$config" --check || return 1
  sudo -n -H -u jarvis-personal-runtime uv run --no-project --python "$candidate/.venv/bin/python" "$base/verify-04.py" || return 1
}
rollback() {
  sudo -n systemctl stop jarvis-personal-runtime || return 1
  sudo -n ln -sfn "$previous" "$base/current.parallel-rollback" || return 1
  sudo -n mv -Tf "$base/current.parallel-rollback" "$base/current" || return 1
  sudo -n systemctl start jarvis-personal-runtime || return 1
  verify_health "$previous"
}
# Prepare the symlink while the previous service is still running. Every later
# failure or signal must recover, validate recovery, and terminate this attempt.
sudo -n ln -sfn "$candidate" "$base/current.parallel-next"
activation_complete=0
on_exit() {
  result=$1
  trap - EXIT HUP INT TERM
  if test "$activation_complete" = 0; then
    if rollback; then
      echo 'Candidate activation failed; previous release restored and health gates passed.' >&2
    else
      echo 'Candidate activation failed; rollback health could not be verified. Inspect the service.' >&2
    fi
    test "$result" != 0 || result=1
  fi
  exit "$result"
}
trap 'on_exit $?' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
sudo -n systemctl stop jarvis-personal-runtime
sudo -n mv -Tf "$base/current.parallel-next" "$base/current"
sudo -n systemctl start jarvis-personal-runtime
verify_health "$candidate"
activation_complete=1
trap - EXIT HUP INT TERM
readlink -f "$base/current"
systemctl show jarvis-personal-runtime -p ActiveState -p SubState -p NRestarts -p MainPID
systemctl is-enabled jarvis-personal-runtime
