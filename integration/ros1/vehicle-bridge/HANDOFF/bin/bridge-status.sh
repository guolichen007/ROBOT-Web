#!/usr/bin/env bash
# Read-only vehicle bridge status. Never reads secrets, never starts/stops the bridge.
# All state is tri-state: YES / NO / UNKNOWN.
set -uo pipefail

UNIT="firebot-bridge"
STATUS_FILE="/run/firebot-bridge/status.json"

echo "== Firebot Bridge status =="

case "$(systemctl is-active "$UNIT" 2>/dev/null || true)" in
  active)   echo "SERVICE_ACTIVE=active" ;;
  inactive) echo "SERVICE_ACTIVE=inactive" ;;
  failed)   echo "SERVICE_ACTIVE=failed" ;;
  *)        echo "SERVICE_ACTIVE=unknown" ;;
esac

PID="$(systemctl show "$UNIT" -p MainPID --value 2>/dev/null || true)"
echo "PID=${PID:-unknown}"
echo "NRESTARTS=$(systemctl show "$UNIT" -p NRestarts --value 2>/dev/null || echo unknown)"

if [ -f "$STATUS_FILE" ] && command -v python3 >/dev/null 2>&1; then
  python3 - "$STATUS_FILE" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    d = {}

def tri(key):
    if key not in d:
        return "UNKNOWN"
    v = d[key]
    if v is True:
        return "YES"
    if v is False:
        return "NO"
    return "UNKNOWN"

def lst(key):
    if key not in d:
        return "UNKNOWN"
    v = d[key]
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return "UNKNOWN"

print("BOOT=" + str(d.get("boot_id") or "UNKNOWN"))
print("MQTT_CONNECTED=" + tri("mqtt_connected"))
print("COMMANDS=" + lst("supported_commands"))
print("SENSORS=" + lst("sensors"))
print("LOCATION_ENABLED=" + tri("location_enabled"))
print("ROS_MASTER_AVAILABLE=" + tri("ros_master_available"))
print("ROS_ADAPTER_READY=" + tri("ros_adapter_ready"))
print("REAL_CONTROL=NOT_IMPLEMENTED")
PY
else
  echo "BOOT=UNKNOWN"
  echo "MQTT_CONNECTED=UNKNOWN"
  echo "COMMANDS=UNKNOWN"
  echo "SENSORS=UNKNOWN"
  echo "LOCATION_ENABLED=UNKNOWN"
  echo "ROS_MASTER_AVAILABLE=UNKNOWN"
  echo "ROS_ADAPTER_READY=UNKNOWN"
  echo "REAL_CONTROL=NOT_IMPLEMENTED"
fi
