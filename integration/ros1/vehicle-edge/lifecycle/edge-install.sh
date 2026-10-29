#!/usr/bin/env bash
# edge-install.sh — firebotctl vehicle edge-install 的受控实现（规格十七/十八）。
#
# 通信专用安装：只安装 Vehicle Edge release + systemd unit + CLI wrappers。
# 绝对不得：touch firerobot_ws/src/firebot_control、catkin_make、安装/启动 firebot-control、
#           启动 ROS/control、发布 cmd_vel（EDGE_INSTALL_NO_CONTROL_TOUCH / NO_CATKIN）。
#
# 前置门（fail-closed）：
#   legacy firebot-bridge inactive（LEGACY_AND_EDGE_MUTUAL_EXCLUSION）
#   new firebot-vehicle-edge inactive（安装 ≠ 启动）
#   repo clean + exact SHA + runtime user 已配置（EDGE_RUNTIME_USER_NOT_CONFIGURED）
#
# 安装完成：firebot-vehicle-edge inactive + disabled。只有 readonly-start 才能启动。
#
# 用法（root）：
#   FIREBOT_REQUIRE_SHA=<SHA> edge-install.sh [REPO_ROOT]
set -euo pipefail

# --help/-h/help 零副作用（规格二十八）：绝不 systemctl / 写文件 / 组装 release。
for a in "$@"; do
  case "$a" in
    -h|--help|help) echo "用法: firebotctl vehicle edge-install --sha <SHA>"; exit 0 ;;
  esac
done

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VEHICLE_EDGE_SRC="$(cd "$SELF_DIR/.." && pwd)"
REPO_ROOT="${1:-$(cd "$VEHICLE_EDGE_SRC/.." && pwd)}"
REQUIRE_SHA="${FIREBOT_REQUIRE_SHA:-}"
EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
# shellcheck disable=SC1091
. "$SELF_DIR/../lib/systemd_state.sh"

# 测试逃逸：仅 CI fixture；生产必须 root（/etc/systemd/system 与权限模型需要）。
if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" != "1" ]; then
  [ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root（用 sudo）" >&2; exit 1; }
fi
echo "$REQUIRE_SHA" | grep -qE '^[0-9a-f]{40}$' || { echo "ERROR: FIREBOT_REQUIRE_SHA 必须 40 位 hex" >&2; exit 1; }

# ---- 前置门 1：完整性来源（生产 tools 模式用 manifest/checksum；源码树用 Git）----
if [ "${FIREBOT_EDGE_TOOLS_MODE:-0}" = "1" ]; then
  # 生产模式（规格十八/十九/二十）：无 Git 依赖。
  # 完整性门 = 当前 tools release 的 manifest source_sha + checksums.sha256。
  # bundled source = 本脚本所在 tools/releases/<SHA>/vehicle-edge。
  TOOLS_ROOT="$(cd "$VEHICLE_EDGE_SRC/.." && pwd)"
  if ! grep -q "\"source_sha\": \"$REQUIRE_SHA\"" "$TOOLS_ROOT/manifest.json" 2>/dev/null; then
    echo "EDGE_INSTALL=REJECTED"
    echo "TOOLS_MANIFEST_INVALID"
    echo "原因：tools manifest source_sha != requested SHA。" >&2
    exit 1
  fi
  if ! (cd "$TOOLS_ROOT" && sha256sum -c checksums.sha256 --quiet >/dev/null 2>&1); then
    echo "EDGE_INSTALL=REJECTED"
    echo "TOOLS_CHECKSUM_INVALID"
    echo "原因：tools release checksums.sha256 校验失败。" >&2
    exit 1
  fi
  echo "TOOLS_MANIFEST_VALID=YES"
  echo "TOOLS_CHECKSUM_VALID=YES"
  echo "TOOLS_SOURCE_SHA=$REQUIRE_SHA"
else
  # 源码树模式（开发/交付 bootstrap）：Git exact-SHA 门。
  if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
    echo "EDGE_INSTALL=REJECTED"
    echo "REPO_NOT_CLEAN"
    exit 1
  fi
  git -C "$REPO_ROOT" cat-file -e "$REQUIRE_SHA^{commit}" 2>/dev/null || {
    echo "EDGE_INSTALL=REJECTED"
    echo "SHA_NOT_PRESENT"
    exit 1
  }
  head_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  [ "$head_sha" = "$REQUIRE_SHA" ] || {
    echo "EDGE_INSTALL=REJECTED"
    echo "HEAD_SHA_MISMATCH"
    exit 1
  }
fi

# ---- 前置门 2：runtime user 已配置（禁止自动猜）----
INSTALL_ENV="$EDGE_HOME/config/install.env"
if [ ! -f "$INSTALL_ENV" ] || ! grep -qE '^FIREBOT_EDGE_USER=[a-z_][a-z0-9_-]*$' "$INSTALL_ENV"; then
  echo "EDGE_INSTALL=REJECTED"
  echo "EDGE_RUNTIME_USER_NOT_CONFIGURED"
  echo "原因：先 firebotctl vehicle edge-init --runtime-user <用户>" >&2
  exit 1
fi
RUNTIME_USER="$(grep -E '^FIREBOT_EDGE_USER=' "$INSTALL_ENV" | cut -d= -f2-)"

# P1-3：显式 ROS adapter 环境注入 systemd（只 source，绝不启动 ROS）
ROS_ENV_LINES=""
ros_setup="$(grep -E '^FIREBOT_ROS_SETUP=' "$INSTALL_ENV" 2>/dev/null | cut -d= -f2- || true)"
ros_ws="$(grep -E '^FIREBOT_ROS_WORKSPACE_SETUP=' "$INSTALL_ENV" 2>/dev/null | cut -d= -f2- || true)"
[ -n "$ros_setup" ] && ROS_ENV_LINES="$ROS_ENV_LINES
Environment=FIREBOT_ROS_SETUP=$ros_setup"
[ -n "$ros_ws" ] && ROS_ENV_LINES="$ROS_ENV_LINES
Environment=FIREBOT_ROS_WORKSPACE_SETUP=$ros_ws"

# ---- 前置门 3：S2 状态（legacy down strict / edge absent或down strict / control safe / tailscale active）----
# P0-3：S2 时旧 Bridge unit 必须仍保留作为 rollback artifact（present + down strict）。
if [ "$(unit_load_state firebot-bridge)" = "not-found" ]; then
  echo "EDGE_INSTALL=REJECTED"
  echo "LEGACY_ROLLBACK_PATH_MISSING"
  echo "原因：firebot-bridge LoadState=not-found（rollback path 缺失）。" >&2
  exit 1
fi
if ! unit_is_down_strict firebot-bridge no; then
  echo "EDGE_INSTALL=REJECTED"
  echo "LEGACY_BRIDGE_ACTIVE"
  echo "原因：firebot-bridge 未 down strict（LEGACY_AND_EDGE_MUTUAL_EXCLUSION）；先 migration-stop-legacy。" >&2
  exit 1
fi
if ! unit_is_down_strict firebot-vehicle-edge yes; then
  echo "EDGE_INSTALL=REJECTED"
  echo "EDGE_SERVICE_ACTIVE"
  echo "原因：firebot-vehicle-edge 未 down strict；安装 ≠ 启动。" >&2
  exit 1
fi
if ! control_is_safe; then
  echo "EDGE_INSTALL=REJECTED"
  echo "CONTROL_NOT_SAFE"
  exit 1
fi
if ! tailscale_is_safe; then
  echo "EDGE_INSTALL=REJECTED"
  echo "TAILSCALE_NOT_SAFE"
  exit 1
fi
# S2 还需公网 MQTT host/port + SUPPORTED_COMMANDS 空（权威来源 bridge.env，enroll 生成）
BRIDGE_ENV_MQTT_HOST="$(grep -E '^FIREBOT_MQTT_HOST=' "$EDGE_HOME/config/bridge.env" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true)"
BRIDGE_ENV_MQTT_PORT="$(grep -E '^FIREBOT_MQTT_PORT=' "$EDGE_HOME/config/bridge.env" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'[:space:]' || echo 8883)"
SUPPORTED_CMDS="$(grep -E '^FIREBOT_SUPPORTED_COMMANDS=' "$EDGE_HOME/config/bridge.env" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true)"
if [ "$BRIDGE_ENV_MQTT_HOST" != "firebot.youdaozd.com" ] || [ "$BRIDGE_ENV_MQTT_PORT" != "8883" ]; then
  echo "EDGE_INSTALL=REJECTED"
  echo "MQTT_ASSIGNMENT_NOT_PUBLIC（host=$BRIDGE_ENV_MQTT_HOST port=$BRIDGE_ENV_MQTT_PORT；期望 firebot.youdaozd.com:8883）" >&2
  exit 1
fi
if [ -n "$SUPPORTED_CMDS" ]; then
  echo "EDGE_INSTALL=REJECTED"
  echo "SUPPORTED_COMMANDS_NOT_EMPTY"
  exit 1
fi

# ---- 组装 release（交付源 = repo 的 vehicle-edge 源码；车辆只是数据）----
if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
  chmod 0755 "$EDGE_HOME/releases" 2>/dev/null || true
fi
bash "$VEHICLE_EDGE_SRC/install/release-manager.sh" build "$VEHICLE_EDGE_SRC" "$REQUIRE_SHA" "$EDGE_HOME"

# ---- 安装 systemd unit（新 unit，绝不改造 legacy unit）----
# 占位符替换用 python（@ROS_ENV_LINES@ 为多行 Environment 行，sed 无法安全处理）
SYSTEMD_DIR="${FIREBOT_TEST_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT_DST="$SYSTEMD_DIR/firebot-vehicle-edge.service"
TMP_UNIT="$(mktemp)"
EDGE_HOME="$EDGE_HOME" RUNTIME_USER="$RUNTIME_USER" ROS_ENV_LINES="$ROS_ENV_LINES" \
python3 - "$VEHICLE_EDGE_SRC/systemd/firebot-vehicle-edge.service.template" "$TMP_UNIT" <<'PY'
import os, sys
src_path, dst_path = sys.argv[1:3]
text = open(src_path, encoding="utf-8").read()
text = text.replace("@EDGE_HOME@", os.environ["EDGE_HOME"])
text = text.replace("@RUNTIME_USER@", os.environ["RUNTIME_USER"])
text = text.replace("@ROS_ENV_LINES@", os.environ.get("ROS_ENV_LINES", ""))
open(dst_path, "w", encoding="utf-8").write(text)
PY
chmod 0644 "$TMP_UNIT"
mv "$TMP_UNIT" "$UNIT_DST"
if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" != "1" ]; then
  "$SYSTEMCTL" daemon-reload
fi

# ---- CLI wrappers（薄转发；禁止重复业务逻辑）----
WRAP_DIR="${FIREBOT_TEST_WRAP_DIR:-/usr/local/bin}"
for cmd in vehicle-connection-status:status vehicle-connection-watch:watch \
           vehicle-connectivity-selftest:selftest vehicle-edge-doctor:doctor; do
  name="${cmd%%:*}"
  sub="${cmd##*:}"
  cat > "$WRAP_DIR/$name" <<EOF
#!/usr/bin/env bash
# 薄 wrapper（等价 firebot-edge $sub）；禁止重复业务逻辑。
exec "$EDGE_HOME/bin/firebot-edge" "$sub" "\$@"
EOF
  chmod 0755 "$WRAP_DIR/$name"
done

# ---- 原子 current/previous 切换（失败 current 不变）----
bash "$VEHICLE_EDGE_SRC/install/release-manager.sh" switch "$REQUIRE_SHA" "$EDGE_HOME"

# ---- 稳定 Edge Runtime 入口（B0-FIX2 / EDGE_STABLE_BIN）----
# bin/firebot-edge -> ../current/bin/firebot-edge（root:root，原子创建）。
# systemd ExecStart 与 /usr/local/bin wrappers 均使用该稳定入口，不直接引用 releases/<SHA>。
STABLE_BIN="$EDGE_HOME/bin/firebot-edge"
STABLE_TARGET="$EDGE_HOME/current/bin/firebot-edge"
if [ ! -x "$STABLE_TARGET" ]; then
  echo "EDGE_INSTALL=FAIL（current/bin/firebot-edge 不存在或不可执行，无法建立稳定入口）" >&2
  exit 1
fi
if [ -e "$STABLE_BIN" ] && [ ! -L "$STABLE_BIN" ]; then
  echo "EDGE_INSTALL=FAIL"
  echo "EDGE_BIN_CONFLICT"
  echo "原因：$STABLE_BIN 已存在且是未知普通文件，禁止静默覆盖。" >&2
  exit 1
fi
if [ -L "$STABLE_BIN" ] && [ "$(readlink "$STABLE_BIN")" = "../current/bin/firebot-edge" ]; then
  echo "EDGE_STABLE_BIN=ALREADY_PRESENT（幂等 PASS）"
else
  mkdir -p "$EDGE_HOME/bin"
  STABLE_TMP="$EDGE_HOME/bin/.firebot-edge.tmp.$$"
  ln -s "../current/bin/firebot-edge" "$STABLE_TMP"
  mv -Tf "$STABLE_TMP" "$STABLE_BIN"
  if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
    chown -h root:root "$STABLE_BIN" 2>/dev/null || true
  fi
  echo "EDGE_STABLE_BIN=PASS"
fi
STABLE_REALPATH="$(readlink -f "$STABLE_BIN" 2>/dev/null || echo UNKNOWN)"
EXPECT_STABLE="$EDGE_HOME/releases/$REQUIRE_SHA/bin/firebot-edge"
if [ "$STABLE_REALPATH" != "$EXPECT_STABLE" ]; then
  echo "EDGE_INSTALL=FAIL（bin/firebot-edge realpath=$STABLE_REALPATH 期望=$EXPECT_STABLE）" >&2
  exit 1
fi
echo "EDGE_STABLE_BIN_RELEASE_SHA=$REQUIRE_SHA"

# ---- 安装后确认：inactive + explicitly-disabled（enabled-runtime/static/unknown 均 FAIL）----
edge_now="$(unit_active_state firebot-vehicle-edge)"
if [ "$edge_now" != "inactive" ] || ! unit_is_explicitly_disabled firebot-vehicle-edge; then
  echo "EDGE_INSTALL=FAIL（安装后 service 必须 inactive+explicit-disabled；实际 active=$edge_now）" >&2
  exit 1
fi

echo "EDGE_INSTALL=PASS sha=$REQUIRE_SHA"
echo "EDGE_INSTALL_NO_CONTROL_TOUCH=PASS"
echo "EDGE_INSTALL_NO_CATKIN=PASS"
echo "EDGE_SERVICE_STATE=inactive+explicit-disabled"
echo "COMMUNICATION_OWNER=NONE"
