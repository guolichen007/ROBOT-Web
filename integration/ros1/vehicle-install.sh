#!/usr/bin/env bash
# vehicle-install.sh — 车端统一安装入口（Bridge + Control + systemd unit）
#
# 用法（必须显式指定批准 SHA）：
#   FIREBOT_REQUIRE_SHA=<FINAL_SHA> ./vehicle-install.sh bridge
#   FIREBOT_REQUIRE_SHA=<FINAL_SHA> ./vehicle-install.sh control
#   FIREBOT_REQUIRE_SHA=<FINAL_SHA> ./vehicle-install.sh all
#   FIREBOT_REQUIRE_SHA=<FINAL_SHA> ./vehicle-install.sh verify
#
# 边界：安装 ≠ 启动。本脚本绝不 enable/start 任何控制服务，绝不修改 supported_commands，
#       绝不运动。firebot-control systemd 单元只安装、默认 disabled，由现场 SOP 手动 start。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_INSTALL="$SCRIPT_DIR/vehicle-bridge/install.sh"
CONTROL_INSTALL="$SCRIPT_DIR/vehicle-control/install.sh"
CONTROL_UNIT_SRC="$SCRIPT_DIR/vehicle-control/systemd/firebot-control.service"
CONTROL_UNIT_DST="/etc/systemd/system/firebot-control.service"
REQUIRE_SHA="${FIREBOT_REQUIRE_SHA:-}"
BRIDGE_DIR="${FIREBOT_INSTALL_DIR:-/opt/firebot/vehicle-bridge}"
CONTROL_DIR="${FIREBOT_ROS_SRC_DIR:-/home/tl/firerobot_ws/src}/firebot_control"

if [ -z "$REQUIRE_SHA" ]; then
  echo "ERROR: 必须设置 FIREBOT_REQUIRE_SHA=<最终批准SHA>，禁止无 SHA 安装" >&2
  exit 1
fi

install_bridge() {
  FIREBOT_REQUIRE_SHA="$REQUIRE_SHA" bash "$BRIDGE_INSTALL"
}

install_control() {
  FIREBOT_REQUIRE_SHA="$REQUIRE_SHA" bash "$CONTROL_INSTALL"
  # 安装 firebot-control systemd 单元（只安装，不 enable/start）
  if [ -f "$CONTROL_UNIT_SRC" ]; then
    if command -v sudo >/dev/null 2>&1; then
      sudo cp "$CONTROL_UNIT_SRC" "$CONTROL_UNIT_DST"
      sudo systemctl daemon-reload
    else
      cp "$CONTROL_UNIT_SRC" "$CONTROL_UNIT_DST"
      systemctl daemon-reload
    fi
    echo "  firebot-control systemd 单元已安装（默认 disabled，不自动启动）"
  fi
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

case "${1:-}" in
  bridge) install_bridge ;;
  control) install_control ;;
  all) install_bridge && install_control ;;
  verify) verify ;;
  *) echo "用法: FIREBOT_REQUIRE_SHA=<SHA> $0 bridge|control|all|verify" >&2; exit 1 ;;
esac
