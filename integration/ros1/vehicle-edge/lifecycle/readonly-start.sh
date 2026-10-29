#!/usr/bin/env bash
# readonly-start.sh（Vehicle Edge 版）— firebotctl vehicle readonly-start 的受控实现。
#
# 现场唯一允许启动 Vehicle Edge 的正式入口（安装 ≠ 启动）。
# S3 → S4：只能从 S3（legacy down strict + edge down strict + control safe +
#   tailscale active + SUPPORTED_COMMANDS 空 + MQTT 公网 host/port + CA canonical）进入。
#
# mutation：START_MUTATION_ISSUED=YES 后经 unit_enable_now_checked 执行 enable --now，
#   四象限：只有 RC=0 + POST_ACTIVE=active + POST_ENABLED=enabled-like 才进入 session 验证；
#   其余立即 fail-closed 停 edge（unit_disable_now_checked），能证明 edge 已停 → FAIL_CLOSED_EDGE_STOP=PASS，
#   否则 FAIL_CLOSED_EDGE_STOP=FAIL + EDGE_STATE=UNCERTAIN + MANUAL_INTERVENTION_REQUIRED=YES。
#
# session 验证（P0-2）：新 status、boot 新鲜、PID==MainPID、release_sha==current、mqtt_connected=true。
# 最终 owner 反证：legacy 仍 down strict + edge active + control safe + tailscale active
#   → STATUS_SESSION_FRESH=YES + COMMUNICATION_OWNER=VEHICLE_EDGE + READONLY_BRIDGE_START=PASS。
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
MQTT_PORT="8883"

SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
STATUS_JSON="${FIREBOT_TEST_STATUS_JSON:-$RUNTIME/status.json}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SELF_DIR/../lib/systemd_state.sh"

FAILED=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILED=1; }
read_kv() {
  grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true
}
utcnow() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ---- S3 前置门（readonly-start 只能从 S3 进入）----
if control_is_safe; then pass "CONTROL_SAFE=YES"; else fail "CONTROL_SAFE=NO"; fi
if tailscale_is_safe; then pass "TAILSCALE_ACTIVE=YES"; else fail "TAILSCALE_ACTIVE=NO"; fi
if unit_is_down_strict firebot-bridge no; then pass "LEGACY_BRIDGE_DOWN_STRICT=yes"; else fail "LEGACY_BRIDGE_DOWN_STRICT=no（必须 loaded+inactive+explicit-disabled）"; fi
if unit_is_down_strict firebot-vehicle-edge no; then pass "EDGE_DOWN_STRICT=yes"; else fail "EDGE_DOWN_STRICT=no（必须 loaded+inactive+explicit-disabled）"; fi

sc="$(read_kv "$CONFIG/bridge.env" FIREBOT_SUPPORTED_COMMANDS)"
[ -z "$sc" ] && pass "SUPPORTED_COMMANDS_EMPTY=yes" || fail "SUPPORTED_COMMANDS_NOT_EMPTY（$sc）"
[ "$(read_kv "$CONFIG/bridge.env" FIREBOT_MQTT_HOST)" = "$MQTT_HOST" ] && pass "MQTT_HOST=$MQTT_HOST" || fail "MQTT_HOST 必须 $MQTT_HOST"
[ "$(read_kv "$CONFIG/bridge.env" FIREBOT_MQTT_PORT)" = "$MQTT_PORT" ] && pass "MQTT_PORT=$MQTT_PORT" || fail "MQTT_PORT 必须 $MQTT_PORT"
[ "$(read_kv "$CONFIG/bridge.env" FIREBOT_CA_CERT)" = "$CERTS/mqtt-ca.crt" ] && pass "MQTT_CA=$CERTS/mqtt-ca.crt" || fail "MQTT_CA 必须 $CERTS/mqtt-ca.crt"

if [ "$FAILED" -ne 0 ]; then
  echo "READONLY_BRIDGE_START=REJECTED（S3 前置门未过）"
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

# ---- mutation（统一 wrapper；四象限）----
echo "START_MUTATION_ISSUED=YES"
unit_enable_now_checked firebot-vehicle-edge
echo "SYSTEMD_MUTATION_RC=$SYSTEMD_MUTATION_RC"

enabled_like=0
case "$POST_ENABLED_STATE" in enabled|enabled-runtime|linked|linked-runtime|alias) enabled_like=1 ;; esac

start_ok=0
if [ "$SYSTEMD_MUTATION_RC" -eq 0 ] && [ "$POST_ACTIVE_STATE" = "active" ] && [ "$enabled_like" -eq 1 ]; then
  start_ok=1
fi

if [ "$start_ok" -ne 1 ]; then
  echo "EDGE_ACTIVE_STATE=$POST_ACTIVE_STATE"
  echo "EDGE_ENABLED_STATE=$POST_ENABLED_STATE"
  echo "EDGE_START_MUTATION=FAIL（RC=$SYSTEMD_MUTATION_RC active=$POST_ACTIVE_STATE enabled=$POST_ENABLED_STATE）"
  # fail-closed：立即停 edge；能证明已停才算 fail-closed stop 成功。
  unit_disable_now_checked firebot-vehicle-edge
  echo "EDGE_ACTIVE_STATE=$POST_ACTIVE_STATE"
  echo "EDGE_ENABLED_STATE=$POST_ENABLED_STATE"
  if [ "$POST_ACTIVE_STATE" = "inactive" ] && unit_is_explicitly_disabled firebot-vehicle-edge; then
    echo "FAIL_CLOSED_EDGE_STOP=PASS"
    echo "EDGE_STOP_VERIFIED=YES"
  else
    echo "FAIL_CLOSED_EDGE_STOP=FAIL"
    echo "EDGE_STATE=UNCERTAIN"
    echo "MANUAL_INTERVENTION_REQUIRED=YES"
  fi
  echo "READONLY_BRIDGE_START=FAIL"
  echo "ROLLBACK_REQUIRED=YES"
  echo "CONTROL_ACTIVE=NO"
  exit 1
fi

# ---- P0-2 新会话验证（stale 检测；有界轮询）----
edge_active="$(unit_active_state firebot-vehicle-edge)"
[ "$edge_active" = "active" ] && pass "BRIDGE_ACTIVE=YES" || fail "BRIDGE_ACTIVE=NO"

main_pid="$(unit_main_pid firebot-vehicle-edge)"
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

start_epoch="$(date -u -d "$START_REQUESTED_AT" +%s 2>/dev/null || echo 0)"
[ "$status_mtime" -ge "$start_epoch" ] && pass "STATUS_NEWER_THAN_START=YES" \
  || fail "STATUS_NEWER_THAN_START=NO（status_mtime=$status_mtime start=$start_epoch）"
[ -n "$boot_id" ] && pass "BOOT_ID_PRESENT=yes" || fail "BOOT_ID_PRESENT=no"
if [ -n "$PREVIOUS_BOOT_ID" ]; then
  [ "$boot_id" != "$PREVIOUS_BOOT_ID" ] && pass "NEW_BOOT_REQUIRED=yes（boot 已变化）" \
    || fail "NEW_BOOT_REQUIRED=no（boot_id 与旧会话相同：stale session）"
fi
[ -n "$status_pid" ] && [ "$status_pid" -gt 0 ] 2>/dev/null && pass "STATUS_PID_POSITIVE=yes" \
  || fail "STATUS_PID_POSITIVE=no（status_pid=$status_pid）"
[ -n "$status_pid" ] && [ -n "$main_pid" ] && [ "$status_pid" = "$main_pid" ] && pass "STATUS_PID_MATCH=yes" \
  || fail "STATUS_PID_MATCH=no（status_pid=$status_pid main_pid=$main_pid）"
[ -n "$current_sha" ] && [ "$status_release" = "$current_sha" ] && pass "STATUS_RELEASE_MATCH=yes" \
  || fail "STATUS_RELEASE_MATCH=no（status_release=$status_release current=$current_sha）"
[ "$mqtt_connected" = "yes" ] && pass "MQTT_CONNECTED=YES" || fail "MQTT_CONNECTED=NO（${MQTT_WAIT}s 内未连接；不发布探测消息）"

if [ "$FAILED" -ne 0 ]; then
  # fail-closed：自动停 edge（统一 wrapper）+ 真实 post-state 输出。
  unit_disable_now_checked firebot-vehicle-edge
  echo "EDGE_ACTIVE_STATE=$POST_ACTIVE_STATE"
  echo "EDGE_ENABLED_STATE=$POST_ENABLED_STATE"
  if [ "$POST_ACTIVE_STATE" = "inactive" ] && unit_is_explicitly_disabled firebot-vehicle-edge; then
    echo "FAIL_CLOSED_EDGE_STOP=PASS"
    echo "EDGE_STOP_VERIFIED=YES"
  else
    echo "FAIL_CLOSED_EDGE_STOP=FAIL"
    echo "EDGE_STATE=UNCERTAIN"
    echo "MANUAL_INTERVENTION_REQUIRED=YES"
  fi
  echo "BRIDGE_ACTIVE=NO"
  echo "READONLY_BRIDGE_START=FAIL"
  echo "ROLLBACK_REQUIRED=YES"
  echo "CONTROL_ACTIVE=NO"
  exit 1
fi

# ---- 最终 owner 反证（绝不双 owner）----
if unit_is_down_strict firebot-bridge no && unit_is_active_strict firebot-vehicle-edge \
   && control_is_safe >/dev/null && tailscale_is_safe >/dev/null; then
  echo "COMMUNICATION_OWNER=VEHICLE_EDGE"
else
  echo "DUAL_COMMUNICATION_OWNER_FORBIDDEN=FAIL"
  echo "READONLY_BRIDGE_START=FAIL"
  echo "ROLLBACK_REQUIRED=YES"
  exit 1
fi

echo "STATUS_SESSION_FRESH=YES"
echo "MQTT_CONNECTED=YES"
echo "READONLY_BRIDGE_START=PASS"
echo "CONTROL_ACTIVE=NO"
