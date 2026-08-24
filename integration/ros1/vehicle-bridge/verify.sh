#!/usr/bin/env bash
# Firebot 车端 Bridge 验收脚本：只输出运行态事实，不伪造 PASS。
set -uo pipefail

BRIDGE_ENV="${FIREBOT_BRIDGE_ENV:-/etc/firebot/bridge.env}"
SECRET_ENV="/etc/firebot/bridge-secret.env"
STATUS_FILE="${FIREBOT_BRIDGE_STATUS_FILE:-/var/run/firebot-bridge/status.json}"
[ -f "$STATUS_FILE" ] || STATUS_FILE="/tmp/firebot-bridge-status.json"

echo "== Firebot Bridge verify =="

# 1) systemd / 进程
if systemctl is-active --quiet firebot-bridge 2>/dev/null; then
  echo "SERVICE_ACTIVE=PASS"
else
  echo "SERVICE_ACTIVE=FAIL"
fi
echo "RESTART_COUNT=$(systemctl show firebot-bridge -p NRestarts --value 2>/dev/null || echo unknown)"
if pgrep -f "python3 -m firebot_bridge.main" >/dev/null 2>&1; then
  echo "PROCESS_ALIVE=PASS"
else
  echo "PROCESS_ALIVE=FAIL"
fi

# 2) 本地 runtime status（Bridge 自己落盘，非 grep 猜测）
if [ -f "$STATUS_FILE" ] && command -v python3 >/dev/null 2>&1; then
  python3 - "$STATUS_FILE" <<'PY'
import json, sys
try:
    s = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
print(f"BOOT_ID={s.get('boot_id') or 'unknown'}")
print(f"MQTT_CONNECTED={'PASS' if s.get('mqtt_connected') else 'NO'}")
print(f"ROS_MASTER_AVAILABLE={'YES' if s.get('ros_master_available') else 'NO'}")
print(f"ROS_ADAPTER_READY={'YES' if s.get('ros_adapter_ready') else 'NO'}")
PY
else
  echo "BOOT_ID=unknown"
  echo "MQTT_CONNECTED=UNKNOWN（无 status 文件）"
  echo "ROS_MASTER_AVAILABLE=UNKNOWN"
  echo "ROS_ADAPTER_READY=UNKNOWN"
fi

# 3) 数据源（从唯一配置读取）
if [ -f "$BRIDGE_ENV" ]; then set -a; . "$BRIDGE_ENV"; set +a; fi
echo "BATTERY_PROVIDER=/firebot_bridge/battery"
if printf '%s' "${FIREBOT_SENSORS:-}" | tr ',' '\n' | grep -qx 'smoke'; then
  echo "SMOKE_PROVIDER=smoke"
else
  echo "SMOKE_PROVIDER=NOT_AVAILABLE"
fi

# 4) 运行模式
echo "STUB_MODE=${BRIDGE_STUB_MODE:-false}"
echo "SUPPORTED_COMMANDS=${FIREBOT_SUPPORTED_COMMANDS:-}"
echo "LOCATION_ENABLED=${FIREBOT_LOCATION_ENABLED:-false}"
echo "SECRET_PRESENT=$([ -f "$SECRET_ENV" ] && echo YES || echo NO)"

# 5) 安全边界
echo "REAL_CONTROL=NOT_IMPLEMENTED"
