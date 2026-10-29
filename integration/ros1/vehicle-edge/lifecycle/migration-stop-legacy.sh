#!/usr/bin/env bash
# migration-stop-legacy.sh — 真实车辆跨 Broker 迁移的正式 cutover 切换点
# （firebotctl vehicle migration-stop-legacy 的受控实现）。
#
# 语义（REAL_R002_CUTOVER_TRANSACTION_FINAL_FIX / P0-1）：
#   - 唯一允许动作：检查 firebot-control disabled/inactive → 检查 firebot-bridge 状态
#     → disable --now firebot-bridge → 确认 firebot-bridge inactive。
#   - Control active/enabled → MIGRATION_STOP_LEGACY=REJECTED / CONTROL_NOT_SAFE（不自动处理 Control）。
#   - 绝对禁止：启动 control/ROS/base/navigation、修改 bridge.env/secret/MQTT credential/Tailscale。
#   - 成功输出 MIGRATION_CUTOVER_BOUNDARY=ENTERED：此后任何失败一律进入 ROLLBACK_REQUIRED。
#
# 用法（车端，收到 enrollment token 之后）：
#   firebotctl vehicle migration-stop-legacy
set -euo pipefail

# --help/-h/help 零副作用（规格二十八）：绝不 systemctl / 停服务 / 进入 cutover。
for a in "$@"; do
  case "$a" in
    -h|--help|help) echo "用法: firebotctl vehicle migration-stop-legacy"; exit 0 ;;
  esac
done

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIREBOT_ETC="${FIREBOT_TEST_ETC_ROOT:-/etc/firebot}"

# 允许测试注入 fixture 命令（生产调用不设置，走真实命令）。
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
# shellcheck disable=SC1091
. "$SELF_DIR/../lib/systemd_state.sh"

# ---- 0) Control 安全门（只检查，绝不自动处理）----
ctl_enabled="$(unit_enabled_state firebot-control)"
ctl_active="$(unit_active_state firebot-control)"
if [ "$ctl_enabled" = "enabled" ] || [ "$ctl_active" = "active" ] || \
   [ "$ctl_active" = "activating" ] || [ "$ctl_active" = "reloading" ]; then
  echo "MIGRATION_STOP_LEGACY=REJECTED"
  echo "CONTROL_NOT_SAFE"
  echo "原因：firebot-control enabled=$ctl_enabled active=$ctl_active；不得自动处理，由人工确认。" >&2
  exit 1
fi

# ---- 1) 记录 legacy Bridge 当前状态 ----
bridge_was_enabled="$(unit_enabled_state firebot-bridge)"
bridge_was_active="$(unit_active_state firebot-bridge)"
[ "$bridge_was_enabled" = "enabled" ] && WAS_ENABLED=YES || WAS_ENABLED=NO
case "$bridge_was_active" in
  active) WAS_ACTIVE=YES ;;
  *) WAS_ACTIVE=NO ;;
esac

# ---- 2) 停止 legacy Bridge（唯一允许的变更动作）----
"$SYSTEMCTL" disable --now firebot-bridge >/dev/null 2>&1 || {
  echo "MIGRATION_STOP_LEGACY=FAIL（firebot-bridge disable --now 失败）" >&2
  exit 1
}
# 立即记录 cutover 已发起：stop 动作已成功发出，此刻起可能已停止旧通信 owner。
# 绝不能在 stop 已发出后输出 CUTOVER_STARTED=NO。
echo "LEGACY_STOP_ACTION_ISSUED=YES"
echo "CUTOVER_STARTED=YES"

# ---- 3) 确认 inactive（fail-closed：无法可靠确定即停止）----
bridge_now="$(unit_active_state firebot-bridge)"
case "$bridge_now" in
  inactive)
    echo "CUTOVER_BOUNDARY_CROSSED=YES"
    echo "LEGACY_BRIDGE_ACTIVE=NO"
    ;;
  *)
    echo "MIGRATION_STOP_LEGACY=UNCERTAIN"
    echo "CUTOVER_STARTED=YES"
    echo "CUTOVER_STATE=UNCERTAIN"
    echo "原因：firebot-bridge 状态无法可靠确定为 inactive（is-active=$bridge_now）；立即停止，不得继续。" >&2
    exit 1
    ;;
esac

echo "LEGACY_BRIDGE_WAS_ACTIVE=$WAS_ACTIVE"
echo "LEGACY_BRIDGE_WAS_ENABLED=$WAS_ENABLED"
echo "LEGACY_BRIDGE_STOPPED=YES"
echo "CONTROL_ACTIVE=NO"
echo "MIGRATION_CUTOVER_BOUNDARY=ENTERED"
