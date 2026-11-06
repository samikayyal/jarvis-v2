#!/bin/sh
set -eu
base=/home/samik/Projects/Jarvis/code
previous=$base/releases/df91f127224c7c5789d6aabbeee15f656139849e
candidate=$base/releases/39765480870245a87abc6ee5d9d689af5d6f8aee
prompt=/home/samik/Projects/Jarvis/other/runtime/SYSTEM.md
backup=/home/samik/Projects/Jarvis/other/runtime/backups/SYSTEM.pre-full-access-3976548.md
test "$(readlink -f "$base/current")" = "$previous"
test -x "$candidate/.venv/bin/jarvis-personal-runtime"
test "$(sudo -n sha256sum "$prompt" | cut -d' ' -f1)" = 33ca22c6f7c7b5b162bd29014139b10ee5b192fec3395136d6bac1672306ba0a
test ! -e "$backup"
sudo -n cp -p "$prompt" "$backup"
rollback() {
  sudo -n systemctl stop jarvis-personal-runtime
  sudo -n cp -p "$backup" "$prompt"
  sudo -n ln -sfn "$previous" "$base/current.full-access-rollback"
  sudo -n mv -Tf "$base/current.full-access-rollback" "$base/current"
  sudo -n systemctl start jarvis-personal-runtime
}
trap 'rollback' HUP INT TERM
sudo -n systemctl stop jarvis-personal-runtime
if ! sudo -n cp "$candidate/deployment/personal-runtime/SYSTEM.md.example" "$prompt"; then rollback; exit 1; fi
sudo -n ln -sfn "$candidate" "$base/current.full-access-next"
if ! sudo -n mv -Tf "$base/current.full-access-next" "$base/current"; then rollback; exit 1; fi
if ! sudo -n systemctl start jarvis-personal-runtime; then rollback; exit 1; fi
sleep 3
if ! sudo -n systemctl is-active --quiet jarvis-personal-runtime; then rollback; exit 1; fi
trap - HUP INT TERM
readlink -f "$base/current"
systemctl show jarvis-personal-runtime -p ActiveState -p SubState -p NRestarts -p MainPID
