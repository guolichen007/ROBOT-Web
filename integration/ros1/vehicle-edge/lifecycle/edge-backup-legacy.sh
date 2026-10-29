#!/usr/bin/env bash
# edge-backup-legacy.sh — firebotctl vehicle edge-backup-legacy 的受控实现。
#
# 正式 Stage A legacy 备份：统一进入 Edge Home
#   /home/tl/firebot-edge/backups/legacy/<UTC_TIMESTAMP>/（root:root 0700）
# 不再 canonical 使用 /root/firebot-legacy-backup-*（P1-4）。
#
# 备份项（探测实证，路径存在才备份；不得猜）：
#   /etc/firebot/、/opt/firebot/vehicle-bridge/（若存在）、
#   firebot-bridge.service / firebot-control.service（若存在）、
#   旧 APPROVED_RUNTIME / release.env、repo HEAD/branch/status、
#   tailscale status --json / ip -4 / ip -6（若 tailscale 存在）
#
# 零运行影响（P1-6）：执行前后 legacy service 状态不变、不 stop/restart/reload/disable
# 任何 runtime service；不改 Tailscale；不输出 password/token/secret/private key。
#
# 用法（root）：edge-backup-legacy.sh [--repo-root <路径>]
set -euo pipefail

# --help/-h/help 零副作用（规格二十八）：绝不 systemctl / 写备份 / 读 tailscale。
for a in "$@"; do
  case "$a" in
    -h|--help|help) echo "用法: firebotctl vehicle edge-backup-legacy [--repo-root <路径>]"; exit 0 ;;
  esac
done

EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
REPO_ROOT="${FIREBOT_REPO_ROOT:-/home/tl/ROBOT-Web}"
# 测试可注入 legacy 路径 fixture（生产默认 /etc/firebot、/opt/firebot/vehicle-bridge）
LEGACY_ETC="${FIREBOT_TEST_LEGACY_ETC:-/etc/firebot}"
LEGACY_BRIDGE="${FIREBOT_TEST_LEGACY_BRIDGE:-/opt/firebot/vehicle-bridge}"
SYSTEMD_UNIT_DIR="${FIREBOT_TEST_SYSTEMD_DIR:-/etc/systemd/system}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SELF_DIR/../lib/systemd_state.sh"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo-root) REPO_ROOT="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" != "1" ]; then
  [ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root（用 sudo）" >&2; exit 1; }
fi

# ---- 前置：记录 legacy 运行状态（零影响断言基线）----
LEGACY_ACTIVE_BEFORE="$(unit_active_state firebot-bridge)"
LEGACY_ENABLED_BEFORE="$(unit_enabled_state firebot-bridge)"
LEGACY_MQTT_HOST_BEFORE="$(grep -E '^FIREBOT_MQTT_HOST=' "$LEGACY_ETC/bridge.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || echo UNKNOWN)"
TAILSCALE_BEFORE="NOT_PRESENT"
if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE_BEFORE="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("Self",{}).get("TailscaleIPs","") or d.get("BackendState",""))' 2>/dev/null || echo PRESENT_UNPARSEABLE)"
fi

# ---- 备份目录（root:root 0700；时间戳）----
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="$EDGE_HOME/backups/legacy/$TS"
mkdir -p "$BACKUP_ROOT"
chmod 0700 "$BACKUP_ROOT" 2>/dev/null || true
if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
  chown root:root "$BACKUP_ROOT" 2>/dev/null || true
fi

# ---- 备份项（探测实证）----
if [ -d "$LEGACY_ETC" ]; then
  cp -a "$LEGACY_ETC" "$BACKUP_ROOT/etc-firebot"
  echo "BACKUP_ITEM=/etc/firebot"
fi
if [ -d "$LEGACY_BRIDGE" ]; then
  cp -a "$LEGACY_BRIDGE" "$BACKUP_ROOT/opt-firebot-vehicle-bridge"
  echo "BACKUP_ITEM=/opt/firebot/vehicle-bridge"
fi
for unit in firebot-bridge.service firebot-control.service; do
  if [ -f "$SYSTEMD_UNIT_DIR/$unit" ]; then
    cp -a "$SYSTEMD_UNIT_DIR/$unit" "$BACKUP_ROOT/$unit"
    echo "BACKUP_ITEM=$SYSTEMD_UNIT_DIR/$unit"
  fi
done
# 旧 APPROVED_RUNTIME / release 信息（非 secret 文本）
{
  echo "APPROVED_RUNTIME=$(cat "$LEGACY_BRIDGE/APPROVED_RUNTIME.txt" 2>/dev/null || echo NONE)"
  echo "RELEASE_ENV_FINAL_SHA=$(grep -E '^FINAL_SHA=' "$LEGACY_ETC/release.env" 2>/dev/null | cut -d= -f2- || echo NONE)"
} > "$BACKUP_ROOT/legacy-release-info.txt"
chmod 0600 "$BACKUP_ROOT/legacy-release-info.txt"
# repo 信息（探测实证：git rev-parse 成功才记录）
if [ "${FIREBOT_EDGE_TOOLS_MODE:-0}" = "1" ]; then
  # 生产 tools 模式：不探测源码仓库（无 Git runtime dependency）。
  echo "REPO_INFO=NOT_APPLICABLE_PRODUCTION" > "$BACKUP_ROOT/repo-info.txt"
  echo "BACKUP_ITEM=repo-info（production：不探测源码仓库）"
elif [ -d "$REPO_ROOT/.git" ] && git -C "$REPO_ROOT" rev-parse HEAD >/dev/null 2>&1; then
  {
    echo "REPO_HEAD=$(git -C "$REPO_ROOT" rev-parse HEAD)"
    echo "REPO_BRANCH=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo detached)"
    git -C "$REPO_ROOT" status --porcelain | head -50 || true
  } > "$BACKUP_ROOT/repo-info.txt"
  echo "BACKUP_ITEM=repo-info（$REPO_ROOT）"
fi
# tailscale（若存在；只记录状态不改动）
if command -v tailscale >/dev/null 2>&1; then
  tailscale status --json > "$BACKUP_ROOT/tailscale-status.json" 2>/dev/null || true
  tailscale ip -4 > "$BACKUP_ROOT/tailscale-ip4.txt" 2>/dev/null || true
  tailscale ip -6 > "$BACKUP_ROOT/tailscale-ip6.txt" 2>/dev/null || true
  echo "BACKUP_ITEM=tailscale-status"
fi
chmod -R go-rwx "$BACKUP_ROOT" 2>/dev/null || true

# ---- 后置：零影响断言（P1-6）----
LEGACY_ACTIVE_AFTER="$(unit_active_state firebot-bridge)"
LEGACY_ENABLED_AFTER="$(unit_enabled_state firebot-bridge)"
LEGACY_MQTT_HOST_AFTER="$(grep -E '^FIREBOT_MQTT_HOST=' "$LEGACY_ETC/bridge.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || echo UNKNOWN)"
if [ "$LEGACY_ACTIVE_BEFORE" = "$LEGACY_ACTIVE_AFTER" ] && [ "$LEGACY_ENABLED_BEFORE" = "$LEGACY_ENABLED_AFTER" ] \
   && [ "$LEGACY_MQTT_HOST_BEFORE" = "$LEGACY_MQTT_HOST_AFTER" ] && [ "$TAILSCALE_BEFORE" = "$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("Self",{}).get("TailscaleIPs","") or d.get("BackendState",""))' 2>/dev/null || echo NOT_PRESENT)" ]; then
  echo "LEGACY_SERVICE_STATE_UNCHANGED=PASS"
else
  echo "LEGACY_SERVICE_STATE_UNCHANGED=FAIL" >&2
  exit 1
fi

echo "LEGACY_BACKUP=PASS"
echo "BACKUP_ROOT=$BACKUP_ROOT"
echo "ZERO_RUNTIME_IMPACT=PASS"
