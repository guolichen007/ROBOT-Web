#!/usr/bin/env bash
# edge-verify.sh — firebotctl vehicle edge-verify 的受控实现。
#
# 验证当前 release：manifest source_sha == FINAL_SHA、layout、关键文件、权限模型、
# service inactive（安装≠启动）、runtime user 配置、bridge.env 与 CA 就绪。
# 只读；不输出任何 secret value。
#
# 用法：FIREBOT_REQUIRE_SHA=<FINAL_SHA> edge-verify.sh [--edge-home …]
set -euo pipefail

# --help/-h/help 零副作用（规格二十八）：绝不 systemctl / 写文件。
for a in "$@"; do
  case "$a" in
    -h|--help|help) echo "用法: firebotctl vehicle edge-verify --sha <SHA>"; exit 0 ;;
  esac
done

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIRE_SHA="${FIREBOT_REQUIRE_SHA:-}"
EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
SYSTEMCTL="${FIREBOT_TEST_SYSTEMCTL:-systemctl}"
# shellcheck disable=SC1091
. "$SELF_DIR/../lib/systemd_state.sh"
while [ $# -gt 0 ]; do
  case "$1" in
    --edge-home) EDGE_HOME="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done
echo "$REQUIRE_SHA" | grep -qE '^[0-9a-f]{40}$' || { echo "ERROR: FIREBOT_REQUIRE_SHA 必须 40 位 hex" >&2; exit 1; }

FAILED=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILED=1; }

# current 指向 releases/<FINAL_SHA>
if [ -L "$EDGE_HOME/current" ] && [ "$(readlink "$EDGE_HOME/current")" = "releases/$REQUIRE_SHA" ]; then
  pass "EDGE_CURRENT_SHA=yes"
else
  fail "EDGE_CURRENT_SHA=no（current=$(readlink "$EDGE_HOME/current" 2>/dev/null || echo 无)）"
fi

# manifest source_sha
manifest="$EDGE_HOME/current/manifest.json"
if grep -q "\"source_sha\": \"$REQUIRE_SHA\"" "$manifest" 2>/dev/null; then
  pass "EDGE_MANIFEST=yes"
else
  fail "EDGE_MANIFEST=no"
fi

# 关键文件
for f in VERSION bin/firebot-edge bridge/run_bridge.sh bridge/firebot_bridge/__init__.py lib/edge_paths.py monitor/health.py; do
  [ -e "$EDGE_HOME/current/$f" ] && pass "EDGE_FILE_$f=yes" || fail "EDGE_FILE_$f=no"
done

# 稳定 Edge Runtime 入口（B0-FIX2 / EDGE_STABLE_BIN）：
# bin/firebot-edge 必须存在、是 symlink、且 readlink -f 解析到 releases/<REQUIRE_SHA>/bin/firebot-edge。
STABLE_BIN="$EDGE_HOME/bin/firebot-edge"
if [ -e "$STABLE_BIN" ]; then
  pass "EDGE_STABLE_BIN_PRESENT=YES"
else
  fail "EDGE_STABLE_BIN_PRESENT=NO（$STABLE_BIN 不存在）"
fi
if [ -L "$STABLE_BIN" ]; then
  pass "EDGE_STABLE_BIN_SYMLINK=YES"
else
  fail "EDGE_STABLE_BIN_SYMLINK=NO（不是 symlink）"
fi
STABLE_REALPATH="$(readlink -f "$STABLE_BIN" 2>/dev/null || echo UNKNOWN)"
EXPECT_STABLE="$EDGE_HOME/releases/$REQUIRE_SHA/bin/firebot-edge"
if [ "$STABLE_REALPATH" = "$EXPECT_STABLE" ] && [ -x "$EXPECT_STABLE" ]; then
  pass "EDGE_STABLE_BIN_RELEASE_SHA=$REQUIRE_SHA"
else
  fail "EDGE_STABLE_BIN_RELEASE_SHA=no（realpath=$STABLE_REALPATH 期望=$EXPECT_STABLE）"
fi

# systemd ExecStart 目标真实存在且可执行（B0-FIX2：不是只查字符串）。
SYSTEMD_UNIT="${FIREBOT_TEST_SYSTEMD_DIR:-/etc/systemd/system}/firebot-vehicle-edge.service"
if grep -q "ExecStart=$EDGE_HOME/bin/firebot-edge run" "$SYSTEMD_UNIT" 2>/dev/null; then
  pass "SYSTEMD_EXEC_START_MATCHES_STABLE_BIN=YES"
else
  fail "SYSTEMD_EXEC_START_MATCHES_STABLE_BIN=NO（unit 未指向 $EDGE_HOME/bin/firebot-edge run）"
fi
if [ -x "$STABLE_REALPATH" ] && [ -f "$STABLE_REALPATH" ]; then
  pass "SYSTEMD_EXEC_TARGET_VALID=YES"
else
  fail "SYSTEMD_EXEC_TARGET_VALID=NO（$STABLE_REALPATH 不存在或不可执行）"
fi

# 权限模型（fail-closed；P1-2 ownership 强校验——runtime user 来自 install.env，禁止硬编码）
owner_is() {
  local path="$1" expect_user="$2" expect_group="${3:-$2}"
  local actual
  actual="$(stat -c '%U:%G' "$path" 2>/dev/null || echo MISSING)"
  [ "$actual" = "$expect_user:$expect_group" ]
}
perm() {
  local path="$1" expect="$2"
  local actual
  actual="$(stat -c %a "$path" 2>/dev/null || echo MISSING)"
  [ "$actual" = "$expect" ]
}
perm "$EDGE_HOME/secrets" 700 && pass "EDGE_PERM_secrets=700" || fail "EDGE_PERM_secrets=700"
perm "$EDGE_HOME/backups" 700 && pass "EDGE_PERM_backups=700" || fail "EDGE_PERM_backups=700"
perm "$EDGE_HOME/runtime" 750 && pass "EDGE_PERM_runtime=750" || fail "EDGE_PERM_runtime=750"
perm "$EDGE_HOME/config/install.env" 644 && pass "EDGE_PERM_install.env=644" || fail "EDGE_PERM_install.env=644"

# P1-2：ownership 强校验（root-owned 目录 root:root；runtime/data/logs = <runtime user>）
EDGE_USER="$(grep -E '^FIREBOT_EDGE_USER=' "$EDGE_HOME/config/install.env" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'[:space:]')"
if [ "${FIREBOT_TEST_SKIP_OWNERSHIP:-0}" = "1" ]; then
  pass "EDGE_OWNERSHIP=SKIPPED（测试逃逸）"
else
  owner_is "$EDGE_HOME" root root && pass "EDGE_HOME_OWNER=root:root" || fail "EDGE_HOME_OWNER=root:root"
  owner_is "$EDGE_HOME/config" root root && pass "CONFIG_OWNER=root:root" || fail "CONFIG_OWNER=root:root"
  owner_is "$EDGE_HOME/certs" root root && pass "CERTS_OWNER=root:root" || fail "CERTS_OWNER=root:root"
  owner_is "$EDGE_HOME/releases" root root && pass "RELEASES_OWNER=root:root" || fail "RELEASES_OWNER=root:root"
  owner_is "$EDGE_HOME/secrets" root root && pass "SECRETS_OWNER=root:root" || fail "SECRETS_OWNER=root:root"
  owner_is "$EDGE_HOME/backups" root root && pass "BACKUPS_OWNER=root:root" || fail "BACKUPS_OWNER=root:root"
  if [ -n "$EDGE_USER" ]; then
    owner_is "$EDGE_HOME/runtime" "$EDGE_USER" "$EDGE_USER" && pass "RUNTIME_OWNER=$EDGE_USER:$EDGE_USER"       || fail "RUNTIME_OWNER=$EDGE_USER:$EDGE_USER"
    owner_is "$EDGE_HOME/data" "$EDGE_USER" "$EDGE_USER" && pass "DATA_OWNER=$EDGE_USER:$EDGE_USER"       || fail "DATA_OWNER=$EDGE_USER:$EDGE_USER"
    owner_is "$EDGE_HOME/logs" "$EDGE_USER" "$EDGE_USER" && pass "LOGS_OWNER=$EDGE_USER:$EDGE_USER"       || fail "LOGS_OWNER=$EDGE_USER:$EDGE_USER"
  else
    fail "EDGE_RUNTIME_USER_NOT_CONFIGURED（无法校验 runtime ownership）"
  fi
fi

# runtime user 配置（禁止自动猜）
grep -qE '^FIREBOT_EDGE_USER=[a-z_][a-z0-9_-]*$' "$EDGE_HOME/config/install.env" 2>/dev/null \
  && pass "EDGE_RUNTIME_USER=configured" || fail "EDGE_RUNTIME_USER=not-configured"

# 安装≠启动：service inactive + explicitly-disabled（enabled-runtime/static/unknown 均 FAIL）
edge_state="$(unit_active_state firebot-vehicle-edge)"
[ "$edge_state" = "inactive" ] && pass "EDGE_SERVICE_INACTIVE=yes" || fail "EDGE_SERVICE_INACTIVE=no（$edge_state）"
if unit_is_explicitly_disabled firebot-vehicle-edge; then
  pass "EDGE_SERVICE_DISABLED=yes"
else
  fail "EDGE_SERVICE_DISABLED=no（UnitFileState 必须 disabled/masked/masked-runtime）"
fi

# P1-4：ROS adapter 环境验证（若配置则文件必须存在；ROS Master 运行不是成功条件）
ros_setup="$(grep -E '^FIREBOT_ROS_SETUP=' "$EDGE_HOME/config/install.env" 2>/dev/null | cut -d= -f2- || true)"
ros_ws="$(grep -E '^FIREBOT_ROS_WORKSPACE_SETUP=' "$EDGE_HOME/config/install.env" 2>/dev/null | cut -d= -f2- || true)"
if [ -n "$ros_setup" ]; then
  [ -f "$ros_setup" ] && pass "ROS_SETUP_READY=YES" || fail "ROS_SETUP_READY=NO（$ros_setup 不存在）"
else
  pass "ROS_SETUP_READY=NOT_CONFIGURED"
fi
if [ -n "$ros_ws" ]; then
  [ -f "$ros_ws" ] && pass "ROS_WORKSPACE_SETUP_READY=YES" || fail "ROS_WORKSPACE_SETUP_READY=NO（$ros_ws 不存在）"
else
  pass "ROS_WORKSPACE_SETUP_READY=NOT_CONFIGURED"
fi

# config/secrets/certs 就绪
[ -f "$EDGE_HOME/config/identity.env" ] && pass "EDGE_IDENTITY=yes" || fail "EDGE_IDENTITY=no"
[ -f "$EDGE_HOME/config/bridge.env" ] && pass "EDGE_BRIDGE_ENV=yes" || fail "EDGE_BRIDGE_ENV=no"
[ -f "$EDGE_HOME/secrets/mqtt.env" ] && pass "EDGE_MQTT_ENV=yes" || fail "EDGE_MQTT_ENV=no"
[ -f "$EDGE_HOME/certs/mqtt-ca.crt" ] && pass "EDGE_MQTT_CA=yes" || fail "EDGE_MQTT_CA=no"

if [ "$FAILED" -ne 0 ]; then
  echo "EDGE_PERMISSION_MODEL=FAIL"
  echo "EDGE_VERIFY=FAIL"
  exit 1
fi
echo "EDGE_PERMISSION_MODEL=PASS"
echo "EDGE_VERIFY=PASS sha=$REQUIRE_SHA"
