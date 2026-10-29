#!/usr/bin/env bash
# install-tools-release.sh — Git → Tools 唯一一次性 bootstrap 安装器（交付时使用）。
#
# 不是长期运行时入口；生产生命周期绝不调用它。
# 目标：把 management plane（firebotctl + lifecycle/install/systemd/config）正式安装进
#   Edge Home tools/releases/<SHA>/，并让 /usr/local/bin/firebotctl 原子指向
#   /home/tl/firebot-edge/tools/current/firebotctl。
#
# 职责（顺序，fail-closed）：
#   1. require root
#   2. require repo clean
#   3. require exact HEAD SHA
#   4. build tools release（tools-release-manager.sh build）
#   5. verify manifest/checksum（tools-release-manager.sh verify）
#   6. switch tools/current（tools-release-manager.sh switch）
#   7. 原子更新 /usr/local/bin/firebotctl → tools/current/firebotctl（最后一步）
#   8. 验证最终 realpath
#   9. 验证 legacy/edge/control/Tailscale 前后状态完全一致（零中断）
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

# ---- 2) repo clean ----
if [ -n "$(git -C "$SOURCE_DIR" status --porcelain 2>/dev/null)" ]; then
  echo "TOOLS_INSTALL=REJECTED"
  echo "REPO_NOT_CLEAN"
  exit 1
fi

# ---- 3) exact HEAD SHA ----
head_sha="$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || echo UNKNOWN)"
[ "$head_sha" = "$SHA" ] || {
  echo "TOOLS_INSTALL=REJECTED"
  echo "HEAD_SHA_MISMATCH"
  echo "原因：git HEAD($head_sha) != --sha($SHA)；先 checkout exact SHA。" >&2
  exit 1
}

# ---- 零中断快照：安装前状态（只读，绝不改动）----
snapshot() {
  local key="$1"; shift
  "$@" 2>/dev/null || echo unknown
}
bridge_before="$(snapshot bridge "$SYSTEMCTL" is-active firebot-bridge 2>/dev/null || echo inactive)"
edge_before="$(snapshot edge "$SYSTEMCTL" is-active firebot-vehicle-edge 2>/dev/null || echo inactive)"
ctl_enabled_before="$(snapshot ctl "$SYSTEMCTL" is-enabled firebot-control 2>/dev/null || echo disabled)"
ctl_active_before="$(snapshot ctl "$SYSTEMCTL" is-active firebot-control 2>/dev/null || echo inactive)"
tailscale_before="$(snapshot ts "$SYSTEMCTL" is-active tailscaled 2>/dev/null || echo inactive)"

# ---- 4) build tools release ----
mkdir -p "$EDGE_HOME/tools/releases" 2>/dev/null || true
bash "$TOOLS_MANAGER" build "$SOURCE_DIR" "$SHA" "$EDGE_HOME"

# ---- 5) verify manifest/checksum ----
bash "$TOOLS_MANAGER" verify "$SHA" "$EDGE_HOME"

# ---- 6) switch tools/current ----
bash "$TOOLS_MANAGER" switch "$SHA" "$EDGE_HOME"

# ---- 7) 原子更新 /usr/local/bin/firebotctl（最后一步；前面失败旧链接不变）----
TARGET="$EDGE_HOME/tools/current/firebotctl"
[ -f "$TARGET" ] || { echo "TOOLS_INSTALL=FAIL（tools/current/firebotctl 不存在）" >&2; exit 1; }
LINK="$BIN_DIR/firebotctl"
TMP_LINK="$BIN_DIR/.firebotctl.tmp.$$"
ln -sfn "$TARGET" "$TMP_LINK"
mv -Tf "$TMP_LINK" "$LINK"

# ---- 8) 验证最终 realpath ----
final_realpath="$(readlink -f "$LINK" 2>/dev/null || echo UNKNOWN)"
expect_realpath="$EDGE_HOME/tools/releases/$SHA/firebotctl"
if [ "$final_realpath" != "$expect_realpath" ]; then
  echo "TOOLS_INSTALL=FAIL（/usr/local/bin/firebotctl realpath=$final_realpath 期望=$expect_realpath）" >&2
  exit 1
fi

# ---- 9) 零中断验证：前后状态完全一致 ----
bridge_after="$(snapshot bridge "$SYSTEMCTL" is-active firebot-bridge 2>/dev/null || echo inactive)"
edge_after="$(snapshot edge "$SYSTEMCTL" is-active firebot-vehicle-edge 2>/dev/null || echo inactive)"
ctl_enabled_after="$(snapshot ctl "$SYSTEMCTL" is-enabled firebot-control 2>/dev/null || echo disabled)"
ctl_active_after="$(snapshot ctl "$SYSTEMCTL" is-active firebot-control 2>/dev/null || echo inactive)"
tailscale_after="$(snapshot ts "$SYSTEMCTL" is-active tailscaled 2>/dev/null || echo inactive)"

[ "$bridge_before" = "$bridge_after" ] && LEGACY_LINK_STATE_UNCHANGED=YES || LEGACY_LINK_STATE_UNCHANGED=NO
[ "$edge_before" = "$edge_after" ] && EDGE_STATE_UNCHANGED=YES || EDGE_STATE_UNCHANGED=NO
[ "$ctl_enabled_before" = "$ctl_enabled_after" ] && [ "$ctl_active_before" = "$ctl_active_after" ] \
  && CONTROL_STATE_UNCHANGED=YES || CONTROL_STATE_UNCHANGED=NO
[ "$tailscale_before" = "$tailscale_after" ] && TAILSCALE_STATE_UNCHANGED=YES || TAILSCALE_STATE_UNCHANGED=NO

echo "TOOLS_INSTALL=PASS sha=$SHA"
echo "FIREBOTCTL_REALPATH=$final_realpath"
echo "LEGACY_LINK_STATE_UNCHANGED=$LEGACY_LINK_STATE_UNCHANGED"
echo "EDGE_STATE_UNCHANGED=$EDGE_STATE_UNCHANGED"
echo "CONTROL_STATE_UNCHANGED=$CONTROL_STATE_UNCHANGED"
echo "TAILSCALE_STATE_UNCHANGED=$TAILSCALE_STATE_UNCHANGED"

if [ "$LEGACY_LINK_STATE_UNCHANGED" != "YES" ] || [ "$EDGE_STATE_UNCHANGED" != "YES" ] \
   || [ "$CONTROL_STATE_UNCHANGED" != "YES" ] || [ "$TAILSCALE_STATE_UNCHANGED" != "YES" ]; then
  echo "TOOLS_INSTALL=FAIL（安装前后服务状态不一致，禁止自动补救）" >&2
  exit 1
fi
