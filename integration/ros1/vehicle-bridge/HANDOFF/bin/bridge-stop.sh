#!/usr/bin/env bash
# Graceful stop via systemd. Never kill -9 / pkill / killall.
set -uo pipefail

UNIT="firebot-bridge"

if [ "$(systemctl is-active "$UNIT" 2>/dev/null || echo inactive)" != "active" ]; then
  echo "PASS: $UNIT not active"
  exit 0
fi

systemctl stop "$UNIT"
echo "STOPPED=$UNIT"
