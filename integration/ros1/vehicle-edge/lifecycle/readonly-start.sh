#!/usr/bin/env bash
# readonly-start.sh（Vehicle Edge 版）— firebotctl vehicle readonly-start 的受控实现。
#
# 现场唯一允许启动 Vehicle Edge 的正式入口（安装 ≠ 启动）。
# 启动前 fail-closed：
#   CONTROL_ENABLED=NO、CONTROL_ACTIVE=NO、SUPPORTED_COMMANDS 空、
#   MQTT_HOST=firebot.youdaozd.com、MQTT_CA=Edge Home certs、
#   legacy firebot-bridge inactive（LEGACY_AND_EDGE_MUTUAL_EXCLUSION）
#
# P0-2 stale session 门（VEHICLE_EDGE_RUNTIME_FRESHNESS_HARDENING）：
#   启动前读旧 status（PREVIOUS_BOOT_ID/PID/MTIME）并原子移动为
#   runtime/status.previous.json；记录 START_REQUESTED_AT；
#   PASS 必须同时证明：service active、新 status 在 START_REQUESTED_AT 后更新、
#   boot_id 非空且 != PREVIOUS_BOOT_ID（若存在）、pid>0、
#   status.pid == systemctl MainPID、release_sha == current release SHA、mqtt_connected=true。
#   全部满足 STATUS_SESSION_FRESH=YES；任一失败 → disable --now +
#   FAIL_CLOSED_BRIDGE_STOP=PASS + ROLLBACK_REQUIRED=YES。绝不 stale PASS。
#
# 绝不触碰 firebot-control/roscore/base/navigation/move_base/cmd_vel/Tailscale。
set -euo pipefail

# --help/-h/help 零副作用（规格二十八）：绝不 systemctl / 写文件 / 启动服务。
for a in "$@"; do
  case "$a" in
    -h|--help|help) echo "用法: firebotctl vehicle readonly-start"; exit 0 ;;
  esac
done

EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
CONFIG="$EDGE_HOME/config"
CERTS="$EDGE_HOME/certs"
RUNTIME="$EDGE_HOME/runtime"
MQTT_HOST="firebot.youdaozd.com"

SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
STATUS_JSON="${FIREBOT_TEST_STATUS_JSON:-$RUNTIME/status.json}"

FAILED=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILED=1; }
read_kv() {
  grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true
}
utcnow() { date -u +%Y-%m-%dT%H:%M:%SZ; }

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

legacy_state="$("$SYSTEMCTL" is-active firebot-bridge 2>/dev/null || echo inactive)"
[ "$legacy_state" = "inactive" ] && pass "LEGACY_BRIDGE_INACTIVE=yes" \
  || fail "LEGACY_BRIDGE_INACTIVE=no（$legacy_state；LEGACY_AND_EDGE_MUTUAL_EXCLUSION）"

if [ "$FAILED" -ne 0 ]; then
  echo "READONLY_BRIDGE_START=REJECTED（只读前置门未过）"
  exit 1
fi

# ---- P0-2：旧 stale status 归档（不得继续作为当前健康状态）----
PREVIOUS_BOOT_ID=""
PREVIOUS_PID=""
PREVIOUS_STATUS_MTIME=""
if [ -f "$STATUS_JSON" ]; then
  PREVIOUS_BOOT_ID="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print(d.get("boot_id") or "")' "$STATUS_JSON" 2>/dev/null || true)"
  PREVIOUS_PID="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print(d.get("pid") or "")' "$STATUS_JSON" 2>/dev/null || true)"
  PREVIOUS_STATUS_MTIME="$(stat -c %Y "$STATUS_JSON" 2>/dev/null || echo 0)"
  mv -f "$STATUS_JSON" "$RUNTIME/status.previous.json" 2>/dev/null || {
    echo "READONLY_BRIDGE_START=FAIL（旧 status 归档失败）" >&2
    exit 1
  }
fi
echo "PREVIOUS_BOOT_ID=${PREVIOUS_BOOT_ID:-<none>}"
echo "PREVIOUS_PID=${PREVIOUS_PID:-<none>}"
START_REQUESTED_AT="$(utcnow)"
echo "START_REQUESTED_AT=$START_REQUESTED_AT"

# ---- 只启动 firebot-vehicle-edge（enable + start；绝不触碰 control/ROS）----
"$SYSTEMCTL" enable --now firebot-vehicle-edge >/dev/null 2>&1 || {
  echo "READONLY_BRIDGE_START=FAIL（firebot-vehicle-edge enable/start 失败）"
  exit 1
}

# ---- P0-2 新会话验证（stale 检测；有界轮询）----
edge_active="$("$SYSTEMCTL" is-active firebot-vehicle-edge 2>/dev/null || echo inactive)"
[ "$edge_active" = "active" ] && pass "BRIDGE_ACTIVE=YES" || fail "BRIDGE_ACTIVE=NO"

main_pid="$("$SYSTEMCTL" show firebot-vehicle-edge -p MainPID --value 2>/dev/null || echo 0)"
current_sha=""
if [ -L "$EDGE_HOME/current" ]; then
  current_sha="$(basename "$(readlink "$EDGE_HOME/current")")"
fi

mqtt_connected="no"
boot_id=""
status_pid=""
status_release=""
status_mtime=""
MQTT_WAIT="${FIREBOT_READONLY_MQTT_WAIT_SECONDS:-30}"
deadline=$((SECONDS + MQTT_WAIT))
while [ "$SECONDS" -lt "$deadline" ]; do
  if [ -f "$STATUS_JSON" ]; then
    mqtt_connected="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print("yes" if d.get("mqtt_connected") is True else "no")' "$STATUS_JSON" 2>/dev/null || echo no)"
    boot_id="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print(d.get("boot_id") or "")' "$STATUS_JSON" 2>/dev/null || true)"
    status_pid="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print(d.get("pid") or "")' "$STATUS_JSON" 2>/dev/null || true)"
    status_release="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print(d.get("release_sha") or "")' "$STATUS_JSON" 2>/dev/null || true)"
    status_mtime="$(stat -c %Y "$STATUS_JSON" 2>/dev/null || echo 0)"
    [ "$mqtt_connected" = "yes" ] && break
  fi
  sleep 2
done

# 新 status 必须在 START_REQUESTED_AT 之后生成/更新
start_epoch="$(date -u -d "$START_REQUESTED_AT" +%s 2>/dev/null || echo 0)"
[ "$status_mtime" -ge "$start_epoch" ] && pass "STATUS_NEWER_THAN_START=YES" \
  || fail "STATUS_NEWER_THAN_START=NO（status_mtime=$status_mtime start=$start_epoch）"

# boot_id 非空且 != PREVIOUS_BOOT_ID（若存在旧会话）
[ -n "$boot_id" ] && pass "BOOT_ID_PRESENT=yes" || fail "BOOT_ID_PRESENT=no"
if [ -n "$PREVIOUS_BOOT_ID" ]; then
  [ "$boot_id" != "$PREVIOUS_BOOT_ID" ] && pass "NEW_BOOT_REQUIRED=yes（boot 已变化）" \
    || fail "NEW_BOOT_REQUIRED=no（boot_id 与旧会话相同：stale session）"
fi

# pid > 0 且 status.pid == MainPID
[ -n "$status_pid" ] && [ "$status_pid" -gt 0 ] 2>/dev/null && pass "STATUS_PID_POSITIVE=yes" \
  || fail "STATUS_PID_POSITIVE=no（status_pid=$status_pid）"
[ -n "$status_pid" ] && [ -n "$main_pid" ] && [ "$status_pid" = "$main_pid" ] && pass "STATUS_PID_MATCH=yes" \
  || fail "STATUS_PID_MATCH=no（status_pid=$status_pid main_pid=$main_pid）"

# release_sha == current release SHA
[ -n "$current_sha" ] && [ "$status_release" = "$current_sha" ] && pass "STATUS_RELEASE_MATCH=yes" \
  || fail "STATUS_RELEASE_MATCH=no（status_release=$status_release current=$current_sha）"

[ "$mqtt_connected" = "yes" ] && pass "MQTT_CONNECTED=YES" || fail "MQTT_CONNECTED=NO（${MQTT_WAIT}s 内未连接；不发布探测消息）"

if [ "$FAILED" -ne 0 ]; then
  # fail-closed：自动退回 inactive（disable + 确认）；绝不触碰 control/ROS/Tailscale
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
  echo "ROLLBACK_REQUIRED=YES"
  echo "CONTROL_ACTIVE=NO"
  exit 1
fi
echo "STATUS_SESSION_FRESH=YES"
echo "READONLY_BRIDGE_START=PASS"
echo "CONTROL_ACTIVE=NO"
