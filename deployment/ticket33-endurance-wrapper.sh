#!/usr/bin/env bash
set -euo pipefail

TICKET33_SOURCE="${TICKET33_SOURCE:-/home/samik/jarvis-ticket33-61992c2}"
TICKET33_PYTHON="${TICKET33_PYTHON:-${TICKET33_SOURCE}/.venv/bin/python}"
TICKET33_EVIDENCE="${TICKET33_EVIDENCE:-/var/lib/jarvis-ticket33-evidence/endurance-eb4f60d.jsonl}"
JARVIS_RELEASE="${JARVIS_RELEASE:-/opt/jarvis/current}"
JARVIS_OVERRIDE="${JARVIS_OVERRIDE:-/etc/jarvis/activation.compose.yaml}"

compose=(
  docker compose
  --file "${JARVIS_RELEASE}/deployment/compose.yaml"
  --file "${JARVIS_OVERRIDE}"
  --profile manual-activation
)

restore_admission() {
  "${compose[@]}" start inbound_receiver
}
trap restore_admission EXIT

test "$(id -u)" -eq 0
test -d "${TICKET33_SOURCE}/.git"
test -x "${TICKET33_PYTHON}"
test ! -e "${TICKET33_EVIDENCE}"
"${compose[@]}" config --quiet
"${compose[@]}" stop inbound_receiver

cd "${TICKET33_SOURCE}"
"${TICKET33_PYTHON}" -m jarvis_control_plane.ticket33_endurance \
  --source-root "${TICKET33_SOURCE}" \
  --python "${TICKET33_PYTHON}" \
  --evidence "${TICKET33_EVIDENCE}" \
  --trace-root /var/lib/jarvis/traces \
  --temporary-root /var/lib/jarvis/tmp
