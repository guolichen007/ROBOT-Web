#!/usr/bin/env bash
# Firebot 车端 Bridge 验收脚本：只输出运行态事实，不伪造 PASS。
set -uo pipefail

INSTALL_DIR="${FIREBOT_INSTALL_DIR:-/opt/firebot/vehicle-bridge}"
ENV_FILE="${FIREBOT_BRIDGE_ENV:-$INSTALL_DIR/config/bridge.env}"
SECRET_ENV="/etc/firebot/bridge-secret.env"

echo "== Firebot Bridge verify =="

# 1) systemd 状态
if systemctl is-active --quiet firebot-bridge 2>/dev/null; then
  echo "SERVICE_ACTIVE=PASS"
else
  echo "SERVICE_ACTIVE=FAIL"
fi
echo "RESTART_COUNT=$(systemctl show firebot-bridge -p NRestarts --value 2>/dev/null || echo unknown)"

# 2) 进程存活
if pgrep -f "python3 -m firebot_bridge.main" >/dev/null 2>&1; then
  echo "PYTHON_ALIVE=PASS"
else
  echo "PYTHON_ALIVE=FAIL"
fi

# 3) MQTT 连接（最近日志）
if journalctl -u firebot-bridge -n 200 --no-pager 2>/dev/null | grep -q "MQTT connected"; then
  echo "MQTT_CONNECTED=PASS"
else
  echo "MQTT_CONNECTED=UNKNOWN"
fi

# 4) boot_id / heartbeat（最近日志）
BOOT_ID=$(journalctl -u firebot-bridge -n 200 --no-pager 2>/dev/null | grep -oE "boot=[0-9a-f]{8}" | head -1 | cut -d= -f2 || true)
echo "BOOT_ID=${BOOT_ID:-unknown}"
if journalctl -u firebot-bridge -n 200 --no-pager 2>/dev/null | grep -q "heartbeat"; then
  echo "HEARTBEAT=PASS"
else
  echo "HEARTBEAT=UNKNOWN"
fi

# 5) 数据源（从配置读取，不判断现场是否有真实 provider 数据）
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
echo "BATTERY_SOURCE=/robot_status.battery_percentage"
echo "SMOKE_SOURCE=/firebot_bridge/smoke（仅当有真实 provider）"
echo "LOCATION_ENABLED=${FIREBOT_LOCATION_ENABLED:-false}"
echo "STUB_MODE=${BRIDGE_STUB_MODE:-false}"
echo "SECRET_PRESENT=$([ -f "$SECRET_ENV" ] && echo YES || echo NO)"

# 6) 安全边界
echo "REAL_CONTROL=NOT_IMPLEMENTED"
