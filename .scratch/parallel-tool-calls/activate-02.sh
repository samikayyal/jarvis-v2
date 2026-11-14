#!/bin/sh
# Activate only after candidate checks, preserving the verified previous release.
set -eu
base=/home/samik/Projects/Jarvis/code
sha=fe70273c5551b2214443d1812365bfa160bc86aa
candidate=$base/releases/$sha
previous=$base/releases/9c2f7d90b867f63cdd536ac0970916ed8143e796
root=/home/samik/Projects/Jarvis/other/runtime
config=/home/samik/Projects/Jarvis/other/config/jarvis.toml
test "$(readlink -f "$base/current")" = "$previous"
# Initial staging stopped at a fixture readiness timeout; preserve that failure.
test "$(cat "$base/parallel-tools-focused-rerun-02.exit")" = 0
test "$(cat "$base/parallel-tools-full-suite-02.exit")" = 0
echo 'd0efea96c32474876fa58449e50ea12306e836fd3ffb0bcadf5113366c87d9ec  '"$config" | sha256sum -c -
echo '9beb38f5530a9bf10fe3159576ee016957ca7227622a2530fd22ca172d3d3db1  '"$root/SYSTEM.md" | sha256sum -c -
verify_source() {
  release=$1
  archive=$2
  sudo -n -H -u jarvis-personal-runtime uv run --no-project --python "$release/.venv/bin/python" "$base/verify-04.py" --sha "${release##*/}" --archive "$archive" --config "$config"
}
verify_source "$candidate" "$base/parallel-tools-$sha.tar.gz"
verify_source "$previous" "$base/readers-continuation.tar.gz"
systemd-analyze verify /etc/systemd/system/jarvis-personal-runtime.service
verify_health() {
  expected=$1
  archive=$2
  attempt=0
  until systemctl is-active --quiet jarvis-personal-runtime &&
    ss -ltnH | awk '$4 == "172.20.0.1:9011" { found=1 } END { exit !found }'; do
    attempt=$((attempt + 1))
    test "$attempt" -lt 45 || return 1
    sleep 1
  done
  systemctl is-enabled --quiet jarvis-personal-runtime || return 1
  test "$(readlink -f "$base/current")" = "$expected" || return 1
  test "$(systemctl show jarvis-personal-runtime -p NRestarts --value)" = 0 || return 1
  sudo -n -H -u jarvis-personal-runtime "$expected/.venv/bin/jarvis-personal-runtime" --root "$root" --config "$config" --check || return 1
  verify_source "$expected" "$archive"
}
rollback() {
  sudo -n systemctl stop jarvis-personal-runtime || return 1
  sudo -n ln -sfn "$previous" "$base/current.tools-rollback" || return 1
  sudo -n mv -Tf "$base/current.tools-rollback" "$base/current" || return 1
  sudo -n systemctl start jarvis-personal-runtime || return 1
  verify_health "$previous" "$base/readers-continuation.tar.gz"
}
activation_complete=0
on_exit() {
  result=$1
  trap - EXIT HUP INT TERM
  if test "$activation_complete" = 0; then
    if rollback; then
      echo 'Activation failed; previous release restored and verified.' >&2
    else
      echo 'Activation failed; rollback health could not be verified.' >&2
    fi
    test "$result" != 0 || result=1
  fi
  exit "$result"
}
sudo -n ln -sfn "$candidate" "$base/current.tools-next"
trap 'on_exit $?' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
sudo -n systemctl stop jarvis-personal-runtime
sudo -n mv -Tf "$base/current.tools-next" "$base/current"
sudo -n systemctl start jarvis-personal-runtime
verify_health "$candidate" "$base/parallel-tools-$sha.tar.gz"
activation_complete=1
trap - EXIT HUP INT TERM
readlink -f "$base/current"
systemctl show jarvis-personal-runtime -p ActiveState -p SubState -p NRestarts -p MainPID
