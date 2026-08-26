#!/usr/bin/env bash
# Live dashboard: refresh status every second. Ctrl+C exits the viewer only; bridge keeps running.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

trap 'echo; echo "viewer exited (bridge still running)"; exit 0' INT

while true; do
  clear
  bash "$SELF_DIR/bridge-status.sh"
  sleep 1
done
