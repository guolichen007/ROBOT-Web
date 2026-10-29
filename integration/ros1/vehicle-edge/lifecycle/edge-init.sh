#!/usr/bin/env bash
# edge-init.sh — firebotctl vehicle edge-init 的受控实现（规格十一）。
#
# 只做：创建 Edge Home 目录树 + 权限初始化 + config/install.env（显式 runtime user）。
# 幂等：重复执行不改变已有正确状态。
#
# 绝不能：生成 credential / 修改 MQTT password / enroll / 启动服务 /
#         停止 legacy Bridge / 修改 Tailscale。
#
# 权限模型（规格九）：
#   EDGE_HOME root:root 0755；releases/config/certs/bin/tools root:root 0755（config 文件 0644）；
#   secrets root:root 0700（文件 0600）；runtime/data/logs <runtime-user> 0750；
#   backups root:root 0700；current/previous 只允许 root 更新。
#
# 用法（root）：
#   firebotctl vehicle edge-init --runtime-user tl
set -euo pipefail

RUNTIME_USER=""
EDGE_HOME="${FIREBOT_EDGE_HOME:-/home/tl/firebot-edge}"
ROS_SETUP=""
ROS_WORKSPACE_SETUP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --runtime-user) RUNTIME_USER="${2:-}"; shift 2 ;;
    --edge-home) EDGE_HOME="${2:-}"; shift 2 ;;
    # P1-3：显式 ROS adapter 环境（Edge 只 source，绝不启动 ROS）
    --ros-setup) ROS_SETUP="${2:-}"; shift 2 ;;
    --ros-workspace-setup) ROS_WORKSPACE_SETUP="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

# 测试逃逸：仅 CI fixture（FIREBOT_TEST_SKIP_ROOT=1）；生产必须 root（权限模型需要）。
if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" != "1" ]; then
  [ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root（用 sudo）" >&2; exit 1; }
fi
[ -n "$RUNTIME_USER" ] || { echo "ERROR: 必须显式 --runtime-user（禁止 id -un / sudo 自动猜）" >&2; exit 1; }
echo "$RUNTIME_USER" | grep -qE '^[a-z_][a-z0-9_-]{0,31}$' || { echo "ERROR: --runtime-user 非法" >&2; exit 1; }
if [ "${FIREBOT_TEST_SKIP_ROOT:-0}" != "1" ]; then
  id "$RUNTIME_USER" >/dev/null 2>&1 || { echo "ERROR: 运行用户 $RUNTIME_USER 不存在（先创建系统账号）" >&2; exit 1; }
fi

verify_perm() {
  local path="$1" expect="$2"
  local actual
  actual="$(stat -c %a "$path" 2>/dev/null || echo MISSING)"
  [ "$actual" = "$expect" ] || { echo "EDGE_INIT=FAIL（$path mode=$actual 期望=$expect）" >&2; exit 1; }
}
ROOT_OWNS="bin config certs releases tools docs"
RW_DIRS="runtime data logs"

mkdir -p "$EDGE_HOME"
chmod 0755 "$EDGE_HOME"
if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
  chown root:root "$EDGE_HOME"
fi
for d in $ROOT_OWNS; do
  mkdir -p "$EDGE_HOME/$d"
  chmod 0755 "$EDGE_HOME/$d"
  if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
    chown root:root "$EDGE_HOME/$d"
  fi
done
# 管理平面 tools 正式 layout（规格十四）：tools/releases + tools/bootstrap 均 root:root 0755。
for d in tools/releases tools/bootstrap; do
  mkdir -p "$EDGE_HOME/$d"
  chmod 0755 "$EDGE_HOME/$d"
  if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
    chown root:root "$EDGE_HOME/$d"
  fi
done
mkdir -p "$EDGE_HOME/secrets" "$EDGE_HOME/backups"
chmod 0700 "$EDGE_HOME/secrets" "$EDGE_HOME/backups"
if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
  chown root:root "$EDGE_HOME/secrets" "$EDGE_HOME/backups"
fi
for d in $RW_DIRS; do
  mkdir -p "$EDGE_HOME/$d"
  if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
    chown "$RUNTIME_USER:$RUNTIME_USER" "$EDGE_HOME/$d"
  fi
  chmod 0750 "$EDGE_HOME/$d"
done

# P1-1/P1-3：ownership/mode 幂等收口（供 EXISTS 场景复用；测试逃逸 SKIP_CHOWN 跳过 chown）
ensure_ownership_model() {
  local home="$1" user="$2"
  chmod 0755 "$home" 2>/dev/null || true
  for d in bin config certs releases tools docs; do
    [ -d "$home/$d" ] || mkdir -p "$home/$d"
    chmod 0755 "$home/$d" 2>/dev/null || true
    if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
      chown root:root "$home/$d" 2>/dev/null || true
    fi
  done
  for d in tools/releases tools/bootstrap; do
    [ -d "$home/$d" ] || mkdir -p "$home/$d"
    chmod 0755 "$home/$d" 2>/dev/null || true
    if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
      chown root:root "$home/$d" 2>/dev/null || true
    fi
  done
  for d in secrets backups; do
    [ -d "$home/$d" ] || mkdir -p "$home/$d"
    chmod 0700 "$home/$d" 2>/dev/null || true
    if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
      chown root:root "$home/$d" 2>/dev/null || true
    fi
  done
  for d in runtime data logs; do
    [ -d "$home/$d" ] || mkdir -p "$home/$d"
    chmod 0750 "$home/$d" 2>/dev/null || true
    if [ "${FIREBOT_TEST_SKIP_CHOWN:-0}" != "1" ]; then
      chown "$user:$user" "$home/$d" 2>/dev/null || true
    fi
  done
}

# install.env：显式 runtime user + Edge Home + 可选 ROS adapter 环境
# （之后所有 lifecycle 读取这里；缺失 fail-closed）
# P1-2：已存在时校验一致性——完全一致 → EXISTS（不重写）；任何差异 → CONFLICT 停止。
# 禁止静默 tl->root、tl->other、home->other root。
INSTALL_ENV="$EDGE_HOME/config/install.env"
if [ -f "$INSTALL_ENV" ]; then
  cur_user="$(grep -E '^FIREBOT_EDGE_USER=' "$INSTALL_ENV" | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]')"
  cur_home="$(grep -E '^FIREBOT_EDGE_HOME=' "$INSTALL_ENV" | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]')"
  cur_ros="$(grep -E '^FIREBOT_ROS_SETUP=' "$INSTALL_ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true)"
  cur_ros_ws="$(grep -E '^FIREBOT_ROS_WORKSPACE_SETUP=' "$INSTALL_ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'[:space:]' || true)"
  if [ "$cur_user" = "$RUNTIME_USER" ] && [ "$cur_home" = "$EDGE_HOME" ] \
     && [ "$cur_ros" = "$ROS_SETUP" ] && [ "$cur_ros_ws" = "$ROS_WORKSPACE_SETUP" ]; then
    # P1-3：config 一致也不直接跳过——先幂等收口 ownership/mode/layout
    # （只修正 owner/mode；runtime user/home/ROS 差异已在上面 CONFLICT 拒绝）
    ensure_ownership_model "$EDGE_HOME" "$RUNTIME_USER"
    verify_perm "$EDGE_HOME" 755
    verify_perm "$EDGE_HOME/secrets" 700
    verify_perm "$EDGE_HOME/backups" 700
    verify_perm "$EDGE_HOME/runtime" 750
    verify_perm "$EDGE_HOME/data" 750
    verify_perm "$EDGE_HOME/logs" 750
    echo "EDGE_INIT=EXISTS"
    echo "EDGE_INIT_IDEMPOTENT=PASS"
    exit 0
  fi
  echo "EDGE_INIT=CONFLICT"
  echo "原因：install.env 已存在且与本次参数不一致（user=$cur_user→$RUNTIME_USER home=$cur_home→$EDGE_HOME）；禁止静默改写。" >&2
  exit 1
fi
TMP_ENV="$(mktemp)"
{
  echo "# firebot-edge install 配置（由 edge-init 生成；禁止手改 runtime user）"
  echo "FIREBOT_EDGE_USER=$RUNTIME_USER"
  echo "FIREBOT_EDGE_HOME=$EDGE_HOME"
  if [ -n "$ROS_SETUP" ]; then
    echo "FIREBOT_ROS_SETUP=$ROS_SETUP"
  fi
  if [ -n "$ROS_WORKSPACE_SETUP" ]; then
    echo "FIREBOT_ROS_WORKSPACE_SETUP=$ROS_WORKSPACE_SETUP"
  fi
} > "$TMP_ENV"
chmod 0644 "$TMP_ENV"
mv "$TMP_ENV" "$INSTALL_ENV"

# 权限校验（fail-closed）

verify_perm "$EDGE_HOME" 755
verify_perm "$EDGE_HOME/secrets" 700
verify_perm "$EDGE_HOME/backups" 700
verify_perm "$EDGE_HOME/runtime" 750
verify_perm "$EDGE_HOME/data" 750
verify_perm "$EDGE_HOME/logs" 750
verify_perm "$INSTALL_ENV" 644

echo "EDGE_INIT=PASS edge_home=$EDGE_HOME runtime_user=$RUNTIME_USER"
echo "EDGE_HOME_LAYOUT=CREATED"
