#!/usr/bin/env bash
# Read-only vehicle bridge status. Never reads secrets, never starts/stops the bridge.
set -uo pipefail

UNIT="firebot-bridge"
STATUS_FILE="/run/firebot-bridge/status.json"

echo "== Firebot Bridge status =="

ACTIVE="$(systemctl is-active "$UNIT" 2>/dev/null || echo unknown)"
echo "SERVICE_ACTIVE=$ACTIVE"

PID="$(systemctl show "$UNIT" -p MainPID --value 2>/dev/null || echo unknown)"
NRESTARTS="$(systemctl show "$UNIT" -p NRestarts --value 2>/dev/null || echo unknown)"
echo "PID=$PID"
echo "NRESTARTS=$NRESTARTS"

if [ -f "$STATUS_FILE" ] && command -v python3 >/dev/null 2>&1; then
  python3 - "$STATUS_FILE" <<'PY'
import json, sys
try:
    s = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    s = {}
def y(v):
    return "YES" if v else "NO"
cmds = s.get("supported_commands") or []
sensors = s.get("sensors") or []
print("BOOT=" + str(s.get("boot_id") or "unknown"))
print("MQTT_CONNECTED=" + ("YES" if s.get("mqtt_connected") else "NO"))
print("COMMANDS=" + (",".join(cmds) if cmds else ""))
print("SENSORS=" + (",".join(sensors) if sensors else ""))
print("LOCATION_ENABLED=" + ("YES" if s.get("location_enabled") else "NO"))
print("ROS_MASTER_AVAILABLE=" + y(s.get("ros_master_available")))
print("ROS_ADAPTER_READY=" + y(s.get("ros_adapter_ready")))
print("REAL_CONTROL=NOT_IMPLEMENTED")
PY
else
  echo "BOOT=unknown"
  echo "MQTT_CONNECTED=unknown"
  echo "COMMANDS=unknown"
  echo "SENSORS=unknown"
  echo "LOCATION_ENABLED=unknown"
  echo "ROS_MASTER_AVAILABLE=unknown"
  echo "ROS_ADAPTER_READY=unknown"
  echo "REAL_CONTROL=NOT_IMPLEMENTED"
fi
