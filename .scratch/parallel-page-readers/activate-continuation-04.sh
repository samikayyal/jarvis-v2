#!/bin/sh
set -eu
base=/home/samik/Projects/Jarvis/code
sha=9c2f7d90b867f63cdd536ac0970916ed8143e796
candidate=$base/releases/$sha
previous=$base/releases/551ed852b16f17f87e149feae1690da39114fc5b
root=/home/samik/Projects/Jarvis/other/runtime
config=/home/samik/Projects/Jarvis/other/config/jarvis.toml
staged=/home/samik/Projects/Jarvis/other/config/jarvis.reader-fix-$sha.toml
backup=/home/samik/Projects/Jarvis/other/config/jarvis.pre-reader-fix-$sha.toml
test "$(readlink -f "$base/current")" = "$previous"
test "$(cat "$base/full-suite-continuation-04.exit")" = 0
test "$(cat "$base/screenshot-continuation-04.exit")" = 0
cmp "$config" "$backup"
cmp "$root/SYSTEM.md" "$candidate/deployment/personal-runtime/SYSTEM.md.example"
sudo -n -H -u jarvis-personal-runtime "$candidate/.venv/bin/jarvis-personal-runtime" --root "$root" --config "$staged" --check
verify_source() {
  sudo -n -H -u jarvis-personal-runtime uv run --no-project --python "$candidate/.venv/bin/python" "$base/verify-04.py" --sha "$sha" --archive "$base/readers-continuation.tar.gz" --config "$1"
}
verify_source "$staged"
systemd-analyze verify /etc/systemd/system/jarvis-personal-runtime.service
verify_health() {
  expected=$1
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
  verify_source "$config"
}
rollback() {
  sudo -n systemctl stop jarvis-personal-runtime || return 1
  sudo -n cp -p "$backup" "$config" || return 1
  sudo -n ln -sfn "$previous" "$base/current.continuation-rollback" || return 1
  sudo -n mv -Tf "$base/current.continuation-rollback" "$base/current" || return 1
  sudo -n systemctl start jarvis-personal-runtime || return 1
  verify_health "$previous"
}
sudo -n ln -sfn "$candidate" "$base/current.continuation-next"
activation_complete=0
on_exit() {
  result=$1
  trap - EXIT HUP INT TERM
  if test "$activation_complete" = 0; then
    if rollback; then
      echo 'Candidate activation failed; previous release and config restored and verified.' >&2
    else
      echo 'Candidate activation failed; rollback health could not be verified.' >&2
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
sudo -n cp -p "$staged" "$config"
sudo -n mv -Tf "$base/current.continuation-next" "$base/current"
sudo -n systemctl start jarvis-personal-runtime
verify_health "$candidate"
activation_complete=1
trap - EXIT HUP INT TERM
readlink -f "$base/current"
systemctl show jarvis-personal-runtime -p ActiveState -p SubState -p NRestarts -p MainPID
