#!/usr/bin/env bash
# rollback.sh（Vehicle Edge 版）— 新链路失败回滚（规格二十二）。
#
# 步骤：
#   1. disable --now firebot-vehicle-edge（新 service 保留 disabled/inactive 作为 artifact）
#   2. 从 backups/legacy/ 恢复 legacy config（/etc/firebot/* 由工具完成或人工按备份清单恢复）
#   3. enable/start firebot-bridge（legacy）
#   4. 验证 MQTT_HOST=100.110.31.112 + MQTT_CONNECTED=YES → LEGACY_ROLLBACK=PASS
#
# 绝不：删除新 service、覆盖旧 service unit、修改云端 identity、touch Tailscale。
# 用法（root）：rollback.sh [--edge-home …]
set -euo pipefail

EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
LEGACY_STATUS="${FIREBOT_TEST_LEGACY_STATUS:-/run/firebot-bridge/status.json}"
LEGACY_BRIDGE_ENV="/etc/firebot/bridge.env"
while [ $# -gt 0 ]; do
  case "$1" in
    --edge-home) EDGE_HOME="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" != "1" ]; then
  [ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root（用 sudo）" >&2; exit 1; }
fi

# 1) 停新 Edge（不删除 service/unit/release）
"$SYSTEMCTL" disable --now firebot-vehicle-edge >/dev/null 2>&1 || true
edge_state="$("$SYSTEMCTL" is-active firebot-vehicle-edge 2>/dev/null || echo inactive)"
if [ "$edge_state" != "inactive" ]; then
  echo "LEGACY_ROLLBACK=FAIL（firebot-vehicle-edge 仍 $edge_state）" >&2
  exit 1
fi
echo "EDGE_SERVICE_STOPPED=YES"

# 2) legacy config 恢复来源提示（backup 清单见 R002 迁移文档；不做静默覆盖）
[ -d "$EDGE_HOME/backups/legacy" ] && echo "LEGACY_BACKUP_FOUND=yes" || echo "LEGACY_BACKUP_FOUND=no（按文档恢复 /etc/firebot/* 后再继续）"

# 3) 启动 legacy bridge
"$SYSTEMCTL" enable --now firebot-bridge >/dev/null 2>&1 || {
  echo "LEGACY_ROLLBACK=FAIL（firebot-bridge enable/start 失败）" >&2
  exit 1
}
legacy_state="$("$SYSTEMCTL" is-active firebot-bridge 2>/dev/null || echo inactive)"
[ "$legacy_state" = "active" ] || { echo "LEGACY_ROLLBACK=FAIL（firebot-bridge 未 active）" >&2; exit 1; }

# 4) 验证回连旧 Broker（只读 status.json；不发探测）
mqtt_host="$(grep -E '^FIREBOT_MQTT_HOST=' "$LEGACY_BRIDGE_ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || echo UNKNOWN)"
mqtt_connected="no"
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if [ -f "$LEGACY_STATUS" ]; then
    mqtt_connected="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print("yes" if d.get("mqtt_connected") is True else "no")' "$LEGACY_STATUS" 2>/dev/null || echo no)"
    [ "$mqtt_connected" = "yes" ] && break
  fi
  sleep 2
done
if [ "$mqtt_host" = "100.110.31.112" ] && [ "$mqtt_connected" = "yes" ]; then
  echo "LEGACY_ROLLBACK=PASS"
  echo "MQTT_HOST=100.110.31.112"
  echo "MQTT_CONNECTED=YES"
else
  echo "LEGACY_ROLLBACK=FAIL（MQTT_HOST=$mqtt_host mqtt_connected=$mqtt_connected）" >&2
  exit 1
fi
