#!/usr/bin/env bash
# migration-stop-legacy.sh — 真实车辆跨 Broker 迁移的正式 cutover 切换点
# （firebotctl vehicle migration-stop-legacy 的受控实现）。
#
# 状态机（S0 → S1）：
#   mutation 前必须全部 PASS：
#     CONTROL_SAFE=YES / TAILSCALE_ACTIVE=YES / LEGACY_LINK_HEALTH=PASS / edge not-found 或 down-strict
#   否则 CUTOVER_STARTED=NO + MIGRATION_STOP_LEGACY=REJECTED（绝不进入 cutover）。
#   全部 PASS 后，mutation 前立即记账 CUTOVER_STARTED=YES + CUTOVER_ACTION=STOP_LEGACY，
#   再经 unit_disable_now_checked 执行 disable --now firebot-bridge。
#   四象限判定：只有 RC=0 + POST_ACTIVE=inactive + POST_ENABLED=explicit-disabled 才 PASS；
#   其余（含 RC!=0 但 post 正确）一律 UNCERTAIN + ROLLBACK_REQUIRED=YES，立即停止。
#
# 绝对禁止：启动 control/ROS/base/navigation、修改 bridge.env/secret/MQTT credential/Tailscale。
#
# 用法（车端，收到 enrollment token 之后）：firebotctl vehicle migration-stop-legacy
set -euo pipefail

# --help/-h/help 零副作用（规格二十八）：绝不 systemctl / 停服务 / 进入 cutover。
for a in "$@"; do
  case "$a" in
    -h|--help|help) echo "用法: firebotctl vehicle migration-stop-legacy"; exit 0 ;;
  esac
done

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIREBOT_ETC="${FIREBOT_TEST_ETC_ROOT:-/etc/firebot}"
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
# shellcheck disable=SC1091
. "$SELF_DIR/../lib/systemd_state.sh"
# shellcheck disable=SC1091
. "$SELF_DIR/../lib/legacy_health.sh"

# ---- 0) 前置状态门（mutation 前必须全部 PASS，否则 CUTOVER_STARTED=NO）----
precheck_fail=0
if control_is_safe; then
  :
else
  precheck_fail=1
  echo "CONTROL_NOT_SAFE"
fi
if tailscale_is_safe; then
  :
else
  precheck_fail=1
  echo "TAILSCALE_NOT_SAFE"
fi
if legacy_link_health; then
  :
else
  precheck_fail=1
fi
if unit_is_down_strict firebot-vehicle-edge yes; then
  :
else
  precheck_fail=1
  echo "EDGE_NOT_DOWN_STRICT"
fi

if [ "$precheck_fail" -ne 0 ]; then
  echo "CUTOVER_STARTED=NO"
  echo "MIGRATION_STOP_LEGACY=REJECTED"
  echo "原因：cutover 前置状态未满足（需 S0：legacy 健康 + control safe + tailscale active + edge down）。" >&2
  exit 1
fi

# ---- 1) mutation 前记录 legacy 状态并立即记账 ----
unit_show_all "firebot-bridge"
case "$UNIT_SHOW_ACTIVE" in active) WAS_ACTIVE=YES ;; *) WAS_ACTIVE=NO ;; esac
case "$UNIT_SHOW_ENABLED" in enabled|enabled-runtime|linked|linked-runtime|alias) WAS_ENABLED=YES ;; *) WAS_ENABLED=NO ;; esac

echo "CUTOVER_STARTED=YES"
echo "CUTOVER_ACTION=STOP_LEGACY"

# ---- 2) mutation（统一 wrapper，暴露 RC + POST_*）----
unit_disable_now_checked firebot-bridge
echo "SYSTEMD_MUTATION_RC=$SYSTEMD_MUTATION_RC"

# ---- 3) 四象限判定 ----
post_disabled=0
case "$POST_ENABLED_STATE" in disabled|masked|masked-runtime) post_disabled=1 ;; esac

if [ "$SYSTEMD_MUTATION_RC" -eq 0 ] && [ "$POST_ACTIVE_STATE" = "inactive" ] && [ "$post_disabled" -eq 1 ]; then
  echo "MIGRATION_STOP_LEGACY=PASS"
  echo "CUTOVER_BOUNDARY_CROSSED=YES"
  echo "COMMUNICATION_OWNER=NONE"
  echo "ROLLBACK_REQUIRED=NO"
  echo "LEGACY_BRIDGE_WAS_ACTIVE=$WAS_ACTIVE"
  echo "LEGACY_BRIDGE_WAS_ENABLED=$WAS_ENABLED"
  echo "LEGACY_BRIDGE_STOPPED=YES"
  echo "CONTROL_ACTIVE=NO"
  echo "MIGRATION_CUTOVER_BOUNDARY=ENTERED"
else
  echo "MIGRATION_STOP_LEGACY=UNCERTAIN"
  echo "CUTOVER_STARTED=YES"
  echo "CUTOVER_STATE=UNCERTAIN"
  echo "ROLLBACK_REQUIRED=YES"
  echo "原因：stop legacy 后状态无法确定为 inactive+explicit-disabled（RC=$SYSTEMD_MUTATION_RC active=$POST_ACTIVE_STATE enabled=$POST_ENABLED_STATE）。" >&2
  exit 1
fi
