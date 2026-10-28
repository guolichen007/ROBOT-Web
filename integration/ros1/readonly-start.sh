#!/usr/bin/env bash
# readonly-start.sh — 真实车辆只读启动（firebotctl vehicle readonly-start 的实现）。
#
# 现场唯一允许启动 Bridge 的正式入口（REAL_VEHICLE_CONNECTIVITY_FIELD_GATE / P0-E）。
#
# 启动前再次 fail-closed 检查：
#   CONTROL_ENABLED=NO、CONTROL_ACTIVE=NO、FIREBOT_SUPPORTED_COMMANDS 为空、
#   MQTT_HOST=firebot.youdaozd.com、MQTT_CA=/etc/firebot/mqtt-ca.crt
# 然后只 enable/start firebot-bridge。
#
# 本脚本绝不触碰 firebot-control / roscore / base / navigation / move_base / cmd_vel。
# 启动后要求 BRIDGE_ACTIVE=YES、MQTT_CONNECTED=YES、boot_id 非空。
#
# 用法（车端）：
#   firebotctl vehicle readonly-start
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 车端固定环境根（测试可经 FIREBOT_TEST_ETC_ROOT 注入 fixture；生产默认 /etc/firebot）
FIREBOT_ETC="${FIREBOT_TEST_ETC_ROOT:-/etc/firebot}"
BRIDGE_ENV="$FIREBOT_ETC/bridge.env"
MQTT_CA="$FIREBOT_ETC/mqtt-ca.crt"
MQTT_HOST="firebot.youdaozd.com"
STATUS_JSON="${FIREBOT_TEST_STATUS_JSON:-/run/firebot-bridge/status.json}"

# 允许测试注入 fixture 命令（生产调用不设置，走真实命令）。
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"

FAILED=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILED=1; }

read_kv() { # read_kv <file> <key> → 值（无则空）
  grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true
}

# ---- 启动前只读铁律门（与 readonly-preflight enrolled 同语义）----
ctl_enabled="$("$SYSTEMCTL" is-enabled firebot-control 2>/dev/null || echo disabled)"
ctl_active="$("$SYSTEMCTL" is-active firebot-control 2>/dev/null || echo inactive)"
[ "$ctl_enabled" = "enabled" ] && fail "CONTROL_ENABLED=yes（禁止启动 Bridge）" || pass "CONTROL_ENABLED=NO"
case "$ctl_active" in
  active|activating|reloading) fail "CONTROL_ACTIVE=yes（禁止启动 Bridge）" ;;
  *) pass "CONTROL_ACTIVE=NO" ;;
esac

sc="$(read_kv "$BRIDGE_ENV" FIREBOT_SUPPORTED_COMMANDS)"
[ -z "$sc" ] && pass "SUPPORTED_COMMANDS_EMPTY=yes" || fail "SUPPORTED_COMMANDS_NOT_EMPTY（$sc）"

[ "$(read_kv "$BRIDGE_ENV" FIREBOT_MQTT_HOST)" = "$MQTT_HOST" ] \
  && pass "MQTT_HOST=$MQTT_HOST" || fail "MQTT_HOST 必须 $MQTT_HOST"
[ "$(read_kv "$BRIDGE_ENV" FIREBOT_CA_CERT)" = "$MQTT_CA" ] \
  && pass "MQTT_CA=$MQTT_CA" || fail "MQTT_CA 必须 $MQTT_CA"

if [ "$FAILED" -ne 0 ]; then
  echo "READONLY_BRIDGE_START=REJECTED（只读前置门未过）"
  exit 1
fi

# ---- 只启动 firebot-bridge（enable + start；绝不触碰 control/roscore/base/navigation）----
"$SYSTEMCTL" enable --now firebot-bridge >/dev/null 2>&1 || {
  echo "READONLY_BRIDGE_START=FAIL（firebot-bridge enable/start 失败）"
  exit 1
}

# ---- 启动后验证：BRIDGE_ACTIVE / MQTT_CONNECTED / boot_id ----
bridge_active="$("$SYSTEMCTL" is-active firebot-bridge 2>/dev/null || echo inactive)"
if [ "$bridge_active" = "active" ]; then
  pass "BRIDGE_ACTIVE=YES"
else
  fail "BRIDGE_ACTIVE=NO（firebot-bridge is-active=$bridge_active）"
fi

sf="$STATUS_JSON"
[ -f "$sf" ] || sf="/tmp/firebot-bridge-status.json"
# Bridge 启动后 MQTT 建连需要时间：有界轮询（最多 30s），不发布任何探测消息。
mqtt_connected="no"
boot_id=""
MQTT_WAIT="${FIREBOT_READONLY_MQTT_WAIT_SECONDS:-30}"
deadline=$((SECONDS + MQTT_WAIT))
while [ "$SECONDS" -lt "$deadline" ]; do
  if [ -f "$sf" ]; then
    mqtt_connected="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print("yes" if d.get("mqtt_connected") is True else "no")' "$sf" 2>/dev/null || echo no)"
    boot_id="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print(d.get("boot_id") or "")' "$sf" 2>/dev/null || true)"
    [ "$mqtt_connected" = "yes" ] && break
  fi
  sleep 2
done
[ "$mqtt_connected" = "yes" ] && pass "MQTT_CONNECTED=YES" || fail "MQTT_CONNECTED=NO（${MQTT_WAIT}s 内 status.json mqtt_connected 非 true；不发布探测消息）"
[ -n "$boot_id" ] && pass "BOOT_ID_PRESENT=yes" || fail "BOOT_ID_PRESENT=no"

if [ "$FAILED" -ne 0 ]; then
  # P0-4 cutover 事务 fail-closed：启动后验收失败时自动退回 inactive，
  # 绝不留下半在线 Bridge（enable 也撤销）。只操作 firebot-bridge；
  # 绝不触碰 firebot-control/ROS/base/navigation/cmd_vel/Tailscale。
  "$SYSTEMCTL" disable --now firebot-bridge >/dev/null 2>&1 || true
  bridge_after="$("$SYSTEMCTL" is-active firebot-bridge 2>/dev/null || echo inactive)"
  bridge_enabled_after="$("$SYSTEMCTL" is-enabled firebot-bridge 2>/dev/null || echo disabled)"
  if [ "$bridge_after" = "inactive" ] && [ "$bridge_enabled_after" != "enabled" ]; then
    echo "FAIL_CLOSED_BRIDGE_STOP=PASS"
  else
    echo "FAIL_CLOSED_BRIDGE_STOP=FAIL（active=$bridge_after enabled=$bridge_enabled_after）"
  fi
  echo "BRIDGE_ACTIVE=NO"
  echo "BRIDGE_ENABLED=NO"
  echo "READONLY_BRIDGE_START=FAIL"
  echo "CONTROL_ACTIVE=NO"
  exit 1
fi
echo "READONLY_BRIDGE_START=PASS"
echo "CONTROL_ACTIVE=NO"
