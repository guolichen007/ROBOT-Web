#!/usr/bin/env bash
# rollback.sh（Vehicle Edge 版）— 通信迁移唯一回滚入口（规格二十四/二十五）。
#
# 语义：cutover 之后（migration-stop-legacy 已执行）新链路失败 → 恢复 legacy 通信 owner。
#
# 步骤（fail-closed）：
#   0. 检查 firebot-control disabled + inactive（不安全 → REJECTED / CONTROL_NOT_SAFE，绝不自动处理）
#   1. 检查 legacy unit（firebot-bridge.service）仍存在
#   2. 检查 legacy bridge.env 仍指 100.110.31.112（漂移 → LEGACY_CONFIG_DRIFTED / MANUAL_RESTORE_REQUIRED=YES）
#   3. 幂等分派（规格二十七 A/B/C/D）
#   4. 需要时 disable --now firebot-vehicle-edge；enable --now firebot-bridge
#   5. 验证 MQTT_HOST=100.110.31.112 + mqtt_connected=true → LEGACY_ROLLBACK=PASS
#
# 绝不：自动覆盖 /etc/firebot/*、自动恢复 secret、catkin_make、修改 Control、修改 Tailscale。
# legacy backup（backups/legacy/）是 DISASTER_RECOVERY_ARTIFACT，不是自动覆盖源。
#
# 用法（root）：rollback.sh [--edge-home …] [--legacy-bridge-env …]
set -euo pipefail

EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
LEGACY_BRIDGE_ENV="${FIREBOT_TEST_LEGACY_BRIDGE_ENV:-/etc/firebot/bridge.env}"
LEGACY_STATUS="${FIREBOT_TEST_LEGACY_STATUS:-/run/firebot-bridge/status.json}"
LEGACY_UNIT="${FIREBOT_TEST_LEGACY_UNIT:-/etc/systemd/system/firebot-bridge.service}"
LEGACY_MQTT_HOST="100.110.31.112"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SELF_DIR/../lib/systemd_state.sh"

# 第二层 --help 零副作用（dispatcher 已拦，此处兜底，绝不执行 gate）。
for a in "$@"; do
  case "$a" in
    -h|--help|help) echo "用法: firebotctl vehicle edge-rollback"; exit 0 ;;
  esac
done

while [ $# -gt 0 ]; do
  case "$1" in
    --edge-home) EDGE_HOME="${2:-}"; shift 2 ;;
    --legacy-bridge-env) LEGACY_BRIDGE_ENV="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" != "1" ]; then
  [ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root（用 sudo）" >&2; exit 1; }
fi

read_kv() {
  grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true
}

# ---- 0) Control 安全门（只检查，绝不自动处理）----
ctl_enabled="$(unit_enabled_state firebot-control)"
ctl_active="$(unit_active_state firebot-control)"
if [ "$ctl_enabled" = "enabled" ] || [ "$ctl_active" = "active" ] || \
   [ "$ctl_active" = "activating" ] || [ "$ctl_active" = "reloading" ]; then
  echo "EDGE_ROLLBACK=REJECTED"
  echo "CONTROL_NOT_SAFE"
  echo "原因：firebot-control enabled=$ctl_enabled active=$ctl_active；不得自动处理，由人工确认。" >&2
  exit 1
fi

# ---- 1) legacy unit 仍存在 ----
if [ ! -f "$LEGACY_UNIT" ]; then
  echo "EDGE_ROLLBACK=REJECTED"
  echo "LEGACY_UNIT_MISSING"
  echo "原因：legacy unit $LEGACY_UNIT 不存在；进入人工灾难恢复。" >&2
  exit 1
fi

# ---- 2) legacy bridge.env 仍指 100.110.31.112（漂移 → 人工恢复，绝不自动覆盖）----
legacy_host="$(read_kv "$LEGACY_BRIDGE_ENV" FIREBOT_MQTT_HOST)"
if [ "$legacy_host" != "$LEGACY_MQTT_HOST" ]; then
  echo "EDGE_ROLLBACK=REJECTED"
  echo "LEGACY_CONFIG_DRIFTED"
  echo "MANUAL_RESTORE_REQUIRED=YES"
  echo "原因：legacy bridge.env MQTT_HOST=$legacy_host（期望 $LEGACY_MQTT_HOST）；禁止自动覆盖，由人工灾难恢复。" >&2
  exit 1
fi

# ---- 3) 幂等分派（规格二十七 A/B/C/D）----
legacy_state="$(unit_active_state firebot-bridge)"
edge_state="$(unit_active_state firebot-vehicle-edge)"
already_rolled_back="no"

case "$edge_state/$legacy_state" in
  active/inactive)    : ;;                        # 情况 A：正常回滚
  inactive/inactive)  : ;;                        # 情况 B：双 inactive → 启动 legacy
  inactive/active)    already_rolled_back="yes" ;; # 情况 C：已回滚
  active/active)
    echo "EDGE_ROLLBACK=REJECTED"
    echo "MUTUAL_EXCLUSION_VIOLATED"
    echo "原因：edge 与 legacy 同时 active（edge=$edge_state legacy=$legacy_state），不自动猜修。" >&2
    exit 1
    ;;
  *)
    echo "EDGE_ROLLBACK=REJECTED"
    echo "UNKNOWN_STATE edge=$edge_state legacy=$legacy_state" >&2
    exit 1
    ;;
esac

# ---- 4) 停新 Edge（不删除 service/unit/release）----
if [ "$edge_state" = "active" ]; then
  "$SYSTEMCTL" disable --now firebot-vehicle-edge >/dev/null 2>&1 || true
  edge_after="$(unit_active_state firebot-vehicle-edge)"
  if [ "$edge_after" != "inactive" ]; then
    echo "EDGE_ROLLBACK=FAIL（firebot-vehicle-edge 仍 $edge_after）" >&2
    exit 1
  fi
  echo "EDGE_SERVICE_STOPPED=YES"
else
  echo "EDGE_SERVICE_STOPPED=NOT_NEEDED"
fi

# ---- 5) 启动 legacy bridge（幂等：active 则跳过）----
if [ "$legacy_state" != "active" ]; then
  "$SYSTEMCTL" enable --now firebot-bridge >/dev/null 2>&1 || {
    echo "EDGE_ROLLBACK=FAIL（firebot-bridge enable/start 失败）" >&2
    exit 1
  }
fi
legacy_now="$(unit_active_state firebot-bridge)"
if [ "$legacy_now" != "active" ]; then
  echo "EDGE_ROLLBACK=FAIL（firebot-bridge 未 active）" >&2
  exit 1
fi

# ---- 6) 验证回连旧 Broker（只读 status.json；不发探测）----
mqtt_connected="no"
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if [ -f "$LEGACY_STATUS" ]; then
    mqtt_connected="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print("yes" if d.get("mqtt_connected") is True else "no")' "$LEGACY_STATUS" 2>/dev/null || echo no)"
    [ "$mqtt_connected" = "yes" ] && break
  fi
  sleep 2
done

if [ "$mqtt_connected" != "yes" ]; then
  echo "EDGE_ROLLBACK=FAIL"
  echo "MQTT_HOST=$legacy_host"
  echo "MQTT_CONNECTED=NO"
  echo "原因：legacy MQTT 未连接（$LEGACY_STATUS mqtt_connected=false）。" >&2
  exit 1
fi

if [ "$already_rolled_back" = "yes" ]; then
  echo "EDGE_ROLLBACK=ALREADY_ROLLED_BACK"
else
  echo "EDGE_ROLLBACK=PASS"
fi
echo "LEGACY_ROLLBACK=PASS"
echo "MQTT_HOST=$LEGACY_MQTT_HOST"
echo "MQTT_CONNECTED=YES"
echo "CONTROL_ACTIVE=NO"
