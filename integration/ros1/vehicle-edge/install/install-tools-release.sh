#!/usr/bin/env bash
# install-tools-release.sh — Git → Tools 唯一一次性 bootstrap 安装器（交付时使用）。
#
# 不是长期运行时入口；生产生命周期绝不调用它。
# 目标：把 management plane（firebotctl + lifecycle/install/systemd/config）正式安装进
#   Edge Home tools/releases/<SHA>/，并让 /usr/local/bin/firebotctl 原子指向
#   /home/tl/firebot-edge/tools/current/firebotctl。
#
# B0-FIX2 事务化（fail-closed）：
#   1. require root + repo clean + exact HEAD SHA
#   2. 记录旧生产管理状态（firebotctl link / tools current / tools previous /
#      legacy state + MQTT host + mqtt_connected / edge state / control enabled+active / tailscale）
#      —— 绝不记录 secret。
#   3. legacy MQTT 健康门：firebot-bridge active 时必须 MQTT_HOST 非空且 mqtt_connected=true，
#      否则 TOOLS_INSTALL=REJECTED / LEGACY_LINK_NOT_HEALTHY。
#   4. build + verify
#   5. 新 release 自检：env -u FIREBOT_EDGE_HOME 下 <release>/firebotctl --version
#      必须 VEHICLE_TOOLS_RELEASE + 正确 source_sha，否则 FAIL / NEW_TOOLING_SELFTEST_FAILED（旧 pointer 不变）。
#   6. switch tools/current，随后 <release>/firebotctl vehicle tooling-status 必须
#      TOOLS_CURRENT_SHA=<SHA> + MANIFEST/CHECKSUM valid + GIT_RUNTIME_DEPENDENCY=NO，
#      否则恢复 tools/current + tools/previous。
#   7. 原子切换 /usr/local/bin/firebotctl → tools/current/firebotctl（最后一步），
#      随后真实执行 /usr/local/bin/firebotctl --version 与 vehicle tooling-status，
#      任一失败 → 恢复 link + tools/current + tools/previous → TOOLS_INSTALL_ROLLED_BACK=YES。
#   8. 零中断验证（含 MQTT）：legacy state/MQTT host/mqtt_connected/edge/control/tailscale 前后完全一致。
#
# 绝对禁止：stop/restart legacy / start edge / start control / 修改 MQTT / enroll / 消费 token。
#
# 用法（交付时一次性；root）：
#   install-tools-release.sh --sha <40SHA> [--edge-home /home/tl/firebot-edge] [--bin-dir /usr/local/bin]
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 交付源 = integration/ros1（firebotctl + vehicle-edge 所在）。
SOURCE_DIR="$(cd "$SELF_DIR/../.." && pwd)"
EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
BIN_DIR="/usr/local/bin"
SHA=""

SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
TOOLS_MANAGER="$SELF_DIR/tools-release-manager.sh"

while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA="${2:-}"; shift 2 ;;
    --edge-home) EDGE_HOME="${2:-}"; shift 2 ;;
    --bin-dir) BIN_DIR="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

# 测试逃逸：仅 CI fixture；生产必须 root（/usr/local/bin 与 tools root:root 权限模型需要）。
if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" != "1" ]; then
  [ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root（用 sudo）" >&2; exit 1; }
fi
echo "$SHA" | grep -qE '^[0-9a-f]{40}$' || { echo "ERROR: --sha 必须 40 位 hex" >&2; exit 1; }

# ---- 1) repo clean + exact HEAD SHA ----
if [ -n "$(git -C "$SOURCE_DIR" status --porcelain 2>/dev/null)" ]; then
  echo "TOOLS_INSTALL=REJECTED"
  echo "REPO_NOT_CLEAN"
  exit 1
fi
head_sha="$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || echo UNKNOWN)"
[ "$head_sha" = "$SHA" ] || {
  echo "TOOLS_INSTALL=REJECTED"
  echo "HEAD_SHA_MISMATCH"
  echo "原因：git HEAD($head_sha) != --sha($SHA)；先 checkout exact SHA。" >&2
  exit 1
}

LINK="$BIN_DIR/firebotctl"
RELEASE_DIR="$EDGE_HOME/tools/releases/$SHA"

# ---- MQTT 状态读取（只读；测试可注入 FIREBOT_TEST_BRIDGE_STATUS_FILE）----
bridge_status_file() {
  if [ -n "${FIREBOT_TEST_BRIDGE_STATUS_FILE:-}" ]; then
    echo "$FIREBOT_TEST_BRIDGE_STATUS_FILE"
    return
  fi
  if [ -f "/run/firebot-bridge/status.json" ]; then
    echo "/run/firebot-bridge/status.json"
  else
    echo "/tmp/firebot-bridge-status.json"
  fi
}
mqtt_host_value() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("mqtt_host") or "")' \
    "$(bridge_status_file)" 2>/dev/null || echo ""
}
mqtt_connected_value() {
  python3 -c 'import json,sys; print("true" if json.load(open(sys.argv[1],encoding="utf-8")).get("mqtt_connected") is True else "false")' \
    "$(bridge_status_file)" 2>/dev/null || echo "false"
}

# shellcheck disable=SC1091
. "$SELF_DIR/../lib/systemd_state.sh"
svc_active() { unit_active_state "$1"; }
svc_enabled() { unit_enabled_state "$1"; }

# ---- 2) 记录旧生产管理状态（只读快照，不记录 secret）----
OLD_FIREBOTCTL_LINK="none"
if [ -L "$LINK" ]; then
  OLD_FIREBOTCTL_LINK="$(readlink "$LINK")"
elif [ -e "$LINK" ]; then
  OLD_FIREBOTCTL_LINK="file"
fi
OLD_TOOLS_CURRENT="none"
[ -L "$EDGE_HOME/tools/current" ] && OLD_TOOLS_CURRENT="$(readlink "$EDGE_HOME/tools/current")"
OLD_TOOLS_PREVIOUS="none"
[ -L "$EDGE_HOME/tools/previous" ] && OLD_TOOLS_PREVIOUS="$(readlink "$EDGE_HOME/tools/previous")"

legacy_state_before="$(svc_active firebot-bridge)"
mqtt_host_before="$(mqtt_host_value)"
mqtt_connected_before="$(mqtt_connected_value)"
edge_state_before="$(svc_active firebot-vehicle-edge)"
ctl_enabled_before="$(svc_enabled firebot-control)"
ctl_active_before="$(svc_active firebot-control)"
tailscale_before="$(svc_active tailscaled)"

# ---- 3) legacy MQTT 健康门（PRE-CUTOVER：legacy active 时必须链路健康）----
if [ "$legacy_state_before" = "active" ]; then
  if [ -z "$mqtt_host_before" ] || [ "$mqtt_connected_before" != "true" ]; then
    echo "TOOLS_INSTALL=REJECTED"
    echo "LEGACY_LINK_NOT_HEALTHY"
    echo "原因：firebot-bridge active 但 MQTT_HOST 为空或 mqtt_connected != true（旧链路已异常，禁止继续 tooling migration）。" >&2
    exit 1
  fi
fi

# ---- 回滚 helper（只恢复管理平面 pointers，绝不触碰任何服务）----
restore_tools_pointers() {
  if [ "$OLD_TOOLS_CURRENT" = "none" ]; then
    rm -f "$EDGE_HOME/tools/current"
  else
    local tmp="$EDGE_HOME/tools/.current.restore.$$"
    ln -s "$OLD_TOOLS_CURRENT" "$tmp"
    mv -Tf "$tmp" "$EDGE_HOME/tools/current"
  fi
  if [ "$OLD_TOOLS_PREVIOUS" = "none" ]; then
    rm -f "$EDGE_HOME/tools/previous"
  else
    local tmp2="$EDGE_HOME/tools/.previous.restore.$$"
    ln -s "$OLD_TOOLS_PREVIOUS" "$tmp2"
    mv -Tf "$tmp2" "$EDGE_HOME/tools/previous"
  fi
}
restore_firebotctl_link() {
  case "$OLD_FIREBOTCTL_LINK" in
    none) rm -f "$LINK" ;;
    file) echo "TOOLS_INSTALL_ROLLBACK=PARTIAL（$LINK 原为普通文件，无法自动恢复）" >&2 ;;
    *) local tmp="$BIN_DIR/.firebotctl.restore.$$"; ln -s "$OLD_FIREBOTCTL_LINK" "$tmp"; mv -Tf "$tmp" "$LINK" ;;
  esac
}

# ---- 4) build + verify ----
mkdir -p "$EDGE_HOME/tools/releases" 2>/dev/null || true
bash "$TOOLS_MANAGER" build "$SOURCE_DIR" "$SHA" "$EDGE_HOME"
bash "$TOOLS_MANAGER" verify "$SHA" "$EDGE_HOME"

# ---- 5) 新 release 自检（真实默认环境：unset FIREBOT_EDGE_HOME）----
selftest_out="$(env -u FIREBOT_EDGE_HOME bash "$RELEASE_DIR/firebotctl" --version 2>&1 || true)"
if [ "${FIREBOT_TEST_FAIL_SELFTEST:-0}" = "1" ] \
   || ! printf '%s' "$selftest_out" | grep -q "FIREBOTCTL_LAYOUT=VEHICLE_TOOLS_RELEASE" \
   || ! printf '%s' "$selftest_out" | grep -q "FIREBOTCTL_SOURCE_SHA=$SHA"; then
  echo "TOOLS_INSTALL=FAIL"
  echo "NEW_TOOLING_SELFTEST_FAILED"
  echo "原因：新 release firebotctl --version 自检未通过（旧 pointer 完全不变）。" >&2
  printf '%s\n' "$selftest_out" >&2
  exit 1
fi

# ---- 6) switch tools/current + 新 release tooling-status 自检 ----
bash "$TOOLS_MANAGER" switch "$SHA" "$EDGE_HOME"
ts_out="$(FIREBOT_EDGE_HOME="$EDGE_HOME" bash "$RELEASE_DIR/firebotctl" vehicle tooling-status 2>&1 || true)"
if [ "${FIREBOT_TEST_FAIL_TOOLING_STATUS:-0}" = "1" ] \
   || ! printf '%s' "$ts_out" | grep -q "TOOLS_CURRENT_SHA=$SHA" \
   || ! printf '%s' "$ts_out" | grep -q "TOOLS_MANIFEST_VALID=YES" \
   || ! printf '%s' "$ts_out" | grep -q "TOOLS_CHECKSUM_VALID=YES" \
   || ! printf '%s' "$ts_out" | grep -q "GIT_RUNTIME_DEPENDENCY=NO"; then
  restore_tools_pointers
  echo "TOOLS_INSTALL=FAIL"
  echo "TOOLS_INSTALL_ROLLED_BACK=YES（tools/current+previous 已恢复）"
  echo "原因：切换后 tooling-status 自检未通过。" >&2
  printf '%s\n' "$ts_out" >&2
  exit 1
fi

# ---- 7) 原子切换 /usr/local/bin/firebotctl（最后一步）+ 真实执行 postcheck ----
TARGET="$EDGE_HOME/tools/current/firebotctl"
[ -f "$TARGET" ] || {
  restore_tools_pointers
  echo "TOOLS_INSTALL=FAIL（tools/current/firebotctl 不存在）" >&2
  exit 1
}
TMP_LINK="$BIN_DIR/.firebotctl.tmp.$$"
ln -sfn "$TARGET" "$TMP_LINK"
mv -Tf "$TMP_LINK" "$LINK"

post_v="$(bash "$LINK" --version 2>&1 || true)"
post_ts="$(bash "$LINK" vehicle tooling-status 2>&1 || true)"
if [ "${FIREBOT_TEST_FAIL_POSTCHECK:-0}" = "1" ] \
   || ! printf '%s' "$post_v" | grep -q "FIREBOTCTL_LAYOUT=VEHICLE_TOOLS_RELEASE" \
   || ! printf '%s' "$post_v" | grep -q "FIREBOTCTL_SOURCE_SHA=$SHA" \
   || ! printf '%s' "$post_ts" | grep -q "TOOLS_CURRENT_SHA=$SHA"; then
  restore_firebotctl_link
  restore_tools_pointers
  echo "TOOLS_INSTALL=FAIL"
  echo "TOOLS_INSTALL_ROLLED_BACK=YES"
  echo "原因：/usr/local/bin/firebotctl 切换后 postcheck 未通过；已恢复 link + tools pointers。" >&2
  exit 1
fi

# ---- 8) 零中断验证（含 MQTT；测试逃逸 FIREBOT_TEST_MQTT_DRIFT 模拟 mqtt_connected 漂移）----
legacy_state_after="$(svc_active firebot-bridge)"
mqtt_host_after="$(mqtt_host_value)"
mqtt_connected_after="$(mqtt_connected_value)"
[ "${FIREBOT_TEST_MQTT_DRIFT:-0}" = "1" ] && mqtt_connected_after="false"
edge_state_after="$(svc_active firebot-vehicle-edge)"
ctl_enabled_after="$(svc_enabled firebot-control)"
ctl_active_after="$(svc_active firebot-control)"
tailscale_after="$(svc_active tailscaled)"

[ "$legacy_state_before" = "$legacy_state_after" ] && LEGACY_BRIDGE_STATE_UNCHANGED=YES || LEGACY_BRIDGE_STATE_UNCHANGED=NO
[ "$mqtt_host_before" = "$mqtt_host_after" ] && LEGACY_MQTT_HOST_UNCHANGED=YES || LEGACY_MQTT_HOST_UNCHANGED=NO
[ "$mqtt_connected_before" = "$mqtt_connected_after" ] && LEGACY_MQTT_CONNECTED_UNCHANGED=YES || LEGACY_MQTT_CONNECTED_UNCHANGED=NO
[ "$edge_state_before" = "$edge_state_after" ] && EDGE_STATE_UNCHANGED=YES || EDGE_STATE_UNCHANGED=NO
[ "$ctl_enabled_before" = "$ctl_enabled_after" ] && CONTROL_ENABLED_UNCHANGED=YES || CONTROL_ENABLED_UNCHANGED=NO
[ "$ctl_active_before" = "$ctl_active_after" ] && CONTROL_ACTIVE_UNCHANGED=YES || CONTROL_ACTIVE_UNCHANGED=NO
[ "$tailscale_before" = "$tailscale_after" ] && TAILSCALE_STATE_UNCHANGED=YES || TAILSCALE_STATE_UNCHANGED=NO

final_realpath="$(readlink -f "$LINK" 2>/dev/null || echo UNKNOWN)"

echo "TOOLS_INSTALL=PASS sha=$SHA"
echo "FIREBOTCTL_REALPATH=$final_realpath"
echo "LEGACY_BRIDGE_STATE_UNCHANGED=$LEGACY_BRIDGE_STATE_UNCHANGED"
echo "LEGACY_MQTT_HOST_UNCHANGED=$LEGACY_MQTT_HOST_UNCHANGED"
echo "LEGACY_MQTT_CONNECTED_UNCHANGED=$LEGACY_MQTT_CONNECTED_UNCHANGED"
echo "EDGE_STATE_UNCHANGED=$EDGE_STATE_UNCHANGED"
echo "CONTROL_ENABLED_UNCHANGED=$CONTROL_ENABLED_UNCHANGED"
echo "CONTROL_ACTIVE_UNCHANGED=$CONTROL_ACTIVE_UNCHANGED"
echo "TAILSCALE_STATE_UNCHANGED=$TAILSCALE_STATE_UNCHANGED"

if [ "$LEGACY_BRIDGE_STATE_UNCHANGED" != "YES" ] || [ "$LEGACY_MQTT_HOST_UNCHANGED" != "YES" ] \
   || [ "$LEGACY_MQTT_CONNECTED_UNCHANGED" != "YES" ] || [ "$EDGE_STATE_UNCHANGED" != "YES" ] \
   || [ "$CONTROL_ENABLED_UNCHANGED" != "YES" ] || [ "$CONTROL_ACTIVE_UNCHANGED" != "YES" ] \
   || [ "$TAILSCALE_STATE_UNCHANGED" != "YES" ]; then
  # B0-FIX3：tools release 已切换但最终 postcondition（零中断）失败 →
  # 恢复管理平面 pointers/link（TOOLS_INSTALL_ROLLED_BACK=YES），
  # 但绝不自动 restart/stop 任何通信服务（COMMUNICATION_STATE_NOT_AUTO_REPAIRED=YES）。
  restore_firebotctl_link
  restore_tools_pointers
  echo "TOOLS_INSTALL=FAIL（安装前后状态不一致）"
  echo "TOOLS_INSTALL_ROLLED_BACK=YES"
  echo "COMMUNICATION_STATE_NOT_AUTO_REPAIRED=YES"
  exit 1
fi
