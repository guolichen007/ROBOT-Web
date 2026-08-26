#!/usr/bin/env bash
# Write a non-secret snapshot to logs/. Never dumps env/secret/token.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SELF_DIR%/bin}/logs"
mkdir -p "$LOG_DIR"

TS="$(date +%Y%m%d-%H%M%S)"
OUT="$LOG_DIR/bridge-snapshot-$TS.txt"

{
  echo "date=$(date -Is)"
  echo "hostname=$(hostname)"
  echo "service=$(systemctl is-active firebot-bridge 2>/dev/null || echo unknown)"
  echo "pid=$(systemctl show firebot-bridge -p MainPID --value 2>/dev/null || echo unknown)"
  echo "nrestarts=$(systemctl show firebot-bridge -p NRestarts --value 2>/dev/null || echo unknown)"
  echo "--- status.json (non-secret fields) ---"
  if [ -f /run/firebot-bridge/status.json ] && command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY' 2>/dev/null || echo "status.json unreadable"
import json
try:
    d = json.load(open("/run/firebot-bridge/status.json", encoding="utf-8"))
except Exception:
    d = {}
for k in ("vehicle_id", "protocol_version", "boot_id", "pid", "mqtt_connected",
          "ros_master_available", "ros_adapter_ready", "stub_mode",
          "field_trace_enabled", "location_enabled", "supported_commands", "sensors"):
    print(k + "=" + str(d.get(k)))
PY
  else
    echo "status.json not found"
  fi
  echo "--- systemctl status (head) ---"
  systemctl status firebot-bridge --no-pager 2>/dev/null | head -20
} > "$OUT"

echo "SNAPSHOT=$OUT"
