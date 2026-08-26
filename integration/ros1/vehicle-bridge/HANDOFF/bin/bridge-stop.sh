#!/usr/bin/env bash
# Graceful stop via systemd. Fail-closed: only PASS after confirmed inactive.
# No forced termination, no signal-based kill, no process-tree kill tools.
set -euo pipefail

UNIT="firebot-bridge"

is_inactive() { [ "$(systemctl is-active "$UNIT" 2>/dev/null || true)" = "inactive" ]; }

if is_inactive; then
  echo "BRIDGE_STOP=PASS (already inactive)"
  exit 0
fi

if ! systemctl stop "$UNIT"; then
  echo "BRIDGE_STOP=FAIL (systemctl stop failed)" >&2
  exit 1
fi

for _ in $(seq 1 20); do
  if is_inactive; then
    echo "BRIDGE_STOP=PASS"
    exit 0
  fi
  sleep 0.5
done

echo "BRIDGE_STOP=FAIL (still not inactive within 10s)" >&2
exit 1
