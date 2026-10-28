#!/usr/bin/env bash
# readonly-start.sh（Vehicle Edge 版）— firebotctl vehicle readonly-start 的受控实现。
#
# 现场唯一允许启动 Vehicle Edge 的正式入口（安装 ≠ 启动）。
# 启动前 fail-closed：
#   CONTROL_ENABLED=NO、CONTROL_ACTIVE=NO、SUPPORTED_COMMANDS 空、
#   MQTT_HOST=firebot.youdaozd.com、MQTT_CA=Edge Home certs、
#   legacy firebot-bridge inactive（LEGACY_AND_EDGE_MUTUAL_EXCLUSION）
# 然后只 enable/start firebot-vehicle-edge。
# 启动后验收失败（ACTIVE/MQTT_CONNECTED/boot_id）→ 自动 disable --now（FAIL_CLOSED_BRIDGE_STOP）。
# 绝不触碰 firebot-control/roscore/base/navigation/move_base/cmd_vel/Tailscale。
set -euo pipefail

EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
CONFIG="$EDGE_HOME/config"
CERTS="$EDGE_HOME/certs"
MQTT_HOST="firebot.youdaozd.com"

SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
STATUS_JSON="${FIREBOT_TEST_STATUS_JSON:-$EDGE_HOME/runtime/status.json}"

FAILED=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILED=1; }
read_kv() {
  grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true
}

# ---- 启动前只读铁律门 ----
ctl_enabled="$("$SYSTEMCTL" is-enabled firebot-control 2>/dev/null || echo disabled)"
ctl_active="$("$SYSTEMCTL" is-active firebot-control 2>/dev/null || echo inactive)"
[ "$ctl_enabled" = "enabled" ] && fail "CONTROL_ENABLED=yes" || pass "CONTROL_ENABLED=NO"
case "$ctl_active" in
  active|activating|reloading) fail "CONTROL_ACTIVE=yes" ;;
  *) pass "CONTROL_ACTIVE=NO" ;;
esac

sc="$(read_kv "$CONFIG/bridge.env" FIREBOT_SUPPORTED_COMMANDS)"
[ -z "$sc" ] && pass "SUPPORTED_COMMANDS_EMPTY=yes" || fail "SUPPORTED_COMMANDS_NOT_EMPTY（$sc）"

[ "$(read_kv "$CONFIG/bridge.env" FIREBOT_MQTT_HOST)" = "$MQTT_HOST" ] \
  && pass "MQTT_HOST=$MQTT_HOST" || fail "MQTT_HOST 必须 $MQTT_HOST"
[ "$(read_kv "$CONFIG/bridge.env" FIREBOT_CA_CERT)" = "$CERTS/mqtt-ca.crt" ] \
  && pass "MQTT_CA=$CERTS/mqtt-ca.crt" || fail "MQTT_CA 必须 $CERTS/mqtt-ca.crt"

# 互斥：legacy bridge 必须 inactive（两个服务不能同时成为 communication owner）
legacy_state="$("$SYSTEMCTL" is-active firebot-bridge 2>/dev/null || echo inactive)"
[ "$legacy_state" = "inactive" ] && pass "LEGACY_BRIDGE_INACTIVE=yes" \
  || fail "LEGACY_BRIDGE_INACTIVE=no（$legacy_state；LEGACY_AND_EDGE_MUTUAL_EXCLUSION）"

if [ "$FAILED" -ne 0 ]; then
  echo "READONLY_BRIDGE_START=REJECTED（只读前置门未过）"
  exit 1
fi

# ---- 只启动 firebot-vehicle-edge（enable + start；绝不触碰 control/ROS）----
"$SYSTEMCTL" enable --now firebot-vehicle-edge >/dev/null 2>&1 || {
  echo "READONLY_BRIDGE_START=FAIL（firebot-vehicle-edge enable/start 失败）"
  exit 1
}

# ---- 启动后验证：BRIDGE_ACTIVE / MQTT_CONNECTED / boot_id（有界轮询；不发探测）----
edge_active="$("$SYSTEMCTL" is-active firebot-vehicle-edge 2>/dev/null || echo inactive)"
[ "$edge_active" = "active" ] && pass "BRIDGE_ACTIVE=YES" || fail "BRIDGE_ACTIVE=NO"

mqtt_connected="no"
boot_id=""
MQTT_WAIT="${FIREBOT_READONLY_MQTT_WAIT_SECONDS:-30}"
deadline=$((SECONDS + MQTT_WAIT))
while [ "$SECONDS" -lt "$deadline" ]; do
  if [ -f "$STATUS_JSON" ]; then
    mqtt_connected="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print("yes" if d.get("mqtt_connected") is True else "no")' "$STATUS_JSON" 2>/dev/null || echo no)"
    boot_id="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print(d.get("boot_id") or "")' "$STATUS_JSON" 2>/dev/null || true)"
    [ "$mqtt_connected" = "yes" ] && break
  fi
  sleep 2
done
[ "$mqtt_connected" = "yes" ] && pass "MQTT_CONNECTED=YES" || fail "MQTT_CONNECTED=NO（${MQTT_WAIT}s 内未连接；不发布探测消息）"
[ -n "$boot_id" ] && pass "BOOT_ID_PRESENT=yes" || fail "BOOT_ID_PRESENT=no"

if [ "$FAILED" -ne 0 ]; then
  # fail-closed：失败自动退回 inactive（disable + 确认）；绝不触碰 control/ROS/Tailscale
  "$SYSTEMCTL" disable --now firebot-vehicle-edge >/dev/null 2>&1 || true
  edge_after="$("$SYSTEMCTL" is-active firebot-vehicle-edge 2>/dev/null || echo inactive)"
  edge_enabled_after="$("$SYSTEMCTL" is-enabled firebot-vehicle-edge 2>/dev/null || echo disabled)"
  if [ "$edge_after" = "inactive" ] && [ "$edge_enabled_after" != "enabled" ]; then
    echo "FAIL_CLOSED_BRIDGE_STOP=PASS"
  else
    echo "FAIL_CLOSED_BRIDGE_STOP=FAIL（active=$edge_after enabled=$edge_enabled_after）"
  fi
  echo "BRIDGE_ACTIVE=NO"
  echo "BRIDGE_ENABLED=NO"
  echo "READONLY_BRIDGE_START=FAIL"
  echo "CONTROL_ACTIVE=NO"
  exit 1
fi
echo "READONLY_BRIDGE_START=PASS"
echo "CONTROL_ACTIVE=NO"
