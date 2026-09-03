#!/usr/bin/env bash
# vehicle-install.sh — 车端统一安装入口（Bridge + Control + systemd unit）
#
# 用法（必须显式指定批准 SHA）：
#   FIREBOT_REQUIRE_SHA=<FINAL_SHA> ./vehicle-install.sh bridge|control|all|verify|rollback
#
# 边界：安装 ≠ 启动。本脚本绝不 enable/start 任何控制服务，绝不修改 supported_commands，
#       绝不运动。firebot-control systemd 单元只安装、默认 disabled。
#       all = Bridge+Control 视为同一 Release 事务：任一失败整体回滚到 OLD SHA。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_INSTALL="$SCRIPT_DIR/vehicle-bridge/install.sh"
CONTROL_INSTALL="$SCRIPT_DIR/vehicle-control/install.sh"
CONTROL_UNIT_SRC="$SCRIPT_DIR/vehicle-control/systemd/firebot-control.service"
CONTROL_UNIT_DST="/etc/systemd/system/firebot-control.service"
REQUIRE_SHA="${FIREBOT_REQUIRE_SHA:-}"
BRIDGE_DIR="${FIREBOT_INSTALL_DIR:-/opt/firebot/vehicle-bridge}"
CONTROL_DIR="${FIREBOT_ROS_SRC_DIR:-/home/tl/firerobot_ws/src}/firebot_control"
RELEASE_ENV="/etc/firebot/release.env"

if [ -z "$REQUIRE_SHA" ]; then
  echo "ERROR: 必须设置 FIREBOT_REQUIRE_SHA=<最终批准SHA>，禁止无 SHA 安装" >&2
  exit 1
fi

# dirty provenance：ROBOT-Web 工作区必须干净（含 untracked），否则安装的代码不可追溯。
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
  echo "ERROR: ROBOT-Web 工作区不干净（含 untracked/staged 变更），禁止安装（dirty provenance）" >&2
  exit 1
fi

install_bridge() {
  FIREBOT_REQUIRE_SHA="$REQUIRE_SHA" bash "$BRIDGE_INSTALL"
}

install_control() {
  FIREBOT_REQUIRE_SHA="$REQUIRE_SHA" bash "$CONTROL_INSTALL"
  # catkin 编译（firebot_control 是 catkin 包，安装后必须编译才能 rosrun）。
  # build 失败自动回滚 previous，绝不留下 new SHA 半安装。
  local ws
  ws="$(dirname "${FIREBOT_ROS_SRC_DIR:-/home/tl/firerobot_ws/src}")"
  echo "  catkin_make ..."
  if ! ( cd "$ws" && catkin_make ); then
    echo "ERROR: catkin_make 失败，自动回滚 previous" >&2
    local pkg="$ws/src/firebot_control" prev="$ws/.firebot_control.previous"
    if [ -d "$prev" ]; then
      mv "$pkg" "$ws/.firebot_control.failed"
      mv "$prev" "$pkg"
      ( cd "$ws" && catkin_make ) || { echo "ERROR: 回滚后 build 也失败" >&2; return 1; }
    else
      echo "ERROR: 无 previous 可回滚" >&2
    fi
    return 1
  fi
  # 安装 firebot-control systemd 单元（只安装，不 enable/start）。
  # 运行用户按 FIREBOT_CONTROL_USER（默认当前用户）生成，不硬编码 tl。
  if [ -f "$CONTROL_UNIT_SRC" ]; then
    local control_user="${FIREBOT_CONTROL_USER:-$(id -un)}"
    local tmp_unit
    tmp_unit="$(mktemp)"
    sed "s/@CONTROL_USER@/$control_user/g" "$CONTROL_UNIT_SRC" > "$tmp_unit"
    if command -v sudo >/dev/null 2>&1; then
      sudo cp "$tmp_unit" "$CONTROL_UNIT_DST"
      sudo systemctl daemon-reload
    else
      cp "$tmp_unit" "$CONTROL_UNIT_DST"
      systemctl daemon-reload
    fi
    rm -f "$tmp_unit"
    echo "  firebot-control systemd 单元已安装（默认 disabled，不自动启动，User=$control_user）"
  fi
}

rollback_bridge() {
  local bridge_dir="${FIREBOT_INSTALL_DIR:-/opt/firebot/vehicle-bridge}"
  local bridge_prev="$(dirname "$bridge_dir")/.$(basename "$bridge_dir").previous"
  [ -d "$bridge_prev" ] || { echo "ERROR: Bridge previous 不存在，无法回滚" >&2; return 1; }
  mv "$bridge_dir" "$(dirname "$bridge_dir")/.$(basename "$bridge_dir").bad" 2>/dev/null || true
  mv "$bridge_prev" "$bridge_dir"
  systemctl restart firebot-bridge 2>/dev/null || { echo "ERROR: Bridge 回滚后 restart 失败" >&2; return 1; }
}

write_release_manifest() {
  local did profile
  did="$(grep -E '^DEVICE_ID=' /etc/firebot/device.env 2>/dev/null | cut -d= -f2- || echo UNKNOWN)"
  profile="$(grep -E '^PROFILE_ID=' /etc/firebot/device.env 2>/dev/null | cut -d= -f2- || echo UNKNOWN)"
  cat > "$RELEASE_ENV" <<EOF
FINAL_SHA=$REQUIRE_SHA
DEVICE_ID=$did
PROFILE_ID=$profile
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  chmod 644 "$RELEASE_ENV"
}

install_all() {
  # Bridge + Control 视为同一 Release 事务：任一失败整体回滚。
  local bridge_old control_old
  bridge_old="$(cat "$BRIDGE_DIR/APPROVED_RUNTIME.txt" 2>/dev/null || echo NONE)"
  control_old="$(cat "$CONTROL_DIR/APPROVED_RUNTIME.txt" 2>/dev/null || echo NONE)"
  if ! install_bridge; then
    echo "VEHICLE_INSTALL=FAIL（Bridge 安装失败）" >&2
    exit 1
  fi
  if ! install_control; then
    echo "VEHICLE_INSTALL=FAIL（Control 安装失败，整体回滚 Bridge）" >&2
    if rollback_bridge; then
      echo "AUTO_ROLLBACK=PASS（Bridge 回 $bridge_old）" >&2
    else
      echo "AUTO_ROLLBACK=FAIL" >&2
    fi
    exit 1
  fi
  write_release_manifest
  echo "VEHICLE_INSTALL=PASS sha=$REQUIRE_SHA（bridge_old=$bridge_old control_old=$control_old）"
}

verify() {
  local bridge_sha control_sha
  bridge_sha="$(cat "$BRIDGE_DIR/APPROVED_RUNTIME.txt" 2>/dev/null || echo MISSING)"
  control_sha="$(cat "$CONTROL_DIR/APPROVED_RUNTIME.txt" 2>/dev/null || echo MISSING)"
  echo "REQUIRE_SHA=$REQUIRE_SHA"
  echo "BRIDGE_APPROVED_RUNTIME=$bridge_sha"
  echo "CONTROL_APPROVED_RUNTIME=$control_sha"
  if [ "$bridge_sha" = "$REQUIRE_SHA" ] && [ "$control_sha" = "$REQUIRE_SHA" ]; then
    echo "VEHICLE_INSTALL_VERIFY=PASS"
    return 0
  fi
  echo "VEHICLE_INSTALL_VERIFY=FAIL" >&2
  return 1
}

rollback() {
  # 严格 fail-closed：previous 缺失/ build 失败/ SHA 不一致都 FAIL，绝不「跳过然后 PASS」。
  local ws
  ws="$(dirname "${FIREBOT_ROS_SRC_DIR:-/home/tl/firerobot_ws/src}")"
  local ctrl_pkg="$ws/src/firebot_control" ctrl_prev="$ws/.firebot_control.previous"
  local bridge_prev_sha ctrl_prev_sha

  if ! rollback_bridge; then
    echo "VEHICLE_ROLLBACK=FAIL（Bridge 回滚失败）" >&2
    exit 1
  fi
  [ -d "$ctrl_prev" ] || { echo "VEHICLE_ROLLBACK=FAIL（Control previous 不存在）" >&2; exit 1; }
  mv "$ctrl_pkg" "$ws/.firebot_control.bad" 2>/dev/null || true
  mv "$ctrl_prev" "$ctrl_pkg"
  ( cd "$ws" && catkin_make ) || { echo "VEHICLE_ROLLBACK=FAIL（Control 回滚后 build 失败）" >&2; exit 1; }

  # 验证两个 runtime SHA 一致且等于 previous SHA
  bridge_prev_sha="$(cat /opt/firebot/vehicle-bridge/APPROVED_RUNTIME.txt 2>/dev/null || echo MISSING)"
  ctrl_prev_sha="$(cat "$ctrl_pkg/APPROVED_RUNTIME.txt" 2>/dev/null || echo MISSING)"
  if [ "$bridge_prev_sha" != "$ctrl_prev_sha" ]; then
    echo "VEHICLE_ROLLBACK=FAIL（Bridge=$bridge_prev_sha != Control=$ctrl_prev_sha）" >&2
    exit 1
  fi
  echo "VEHICLE_ROLLBACK=PASS previous=$bridge_prev_sha"
}

case "${1:-}" in
  bridge) install_bridge ;;
  control) install_control ;;
  all) install_all ;;
  verify) verify ;;
  rollback) rollback ;;
  *) echo "用法: FIREBOT_REQUIRE_SHA=<SHA> $0 bridge|control|all|verify|rollback" >&2; exit 1 ;;
esac
