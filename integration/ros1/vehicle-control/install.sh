#!/usr/bin/env bash
# Firebot 车端 firebot_control 安装脚本（GitHub 是唯一交付源）
#
# 可配置：
#   FIREBOT_ROS_SRC_DIR   ROS 工作区 src 目录（默认 /home/tl/firerobot_ws/src）
#   FIREBOT_REQUIRE_SHA  可选，强制校验源码 HEAD，防止装错版本
#
# 边界：只同步 firebot_control 这一个包，不覆盖 src 目录下其它 autocar 工作树文件；
#       不处理/不写入任何 secret。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROS_SRC_DIR="${FIREBOT_ROS_SRC_DIR:-/home/tl/firerobot_ws/src}"
PKG_DIR="$ROS_SRC_DIR/firebot_control"

echo "== firebot_control 安装 =="
echo "  ROS_SRC_DIR=$ROS_SRC_DIR"
echo "  部署目录=$PKG_DIR"

# 源码 SHA：与 Bridge 同一套 ROBOT-Web 权威；支持强制校验 + 安装后留痕。
if [ -n "${FIREBOT_REQUIRE_SHA:-}" ]; then
  SOURCE_SHA="$FIREBOT_REQUIRE_SHA"
  actual="$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || echo "")"
  if [ "$actual" != "$SOURCE_SHA" ]; then
    echo "ERROR: 源码 SHA 与 FIREBOT_REQUIRE_SHA 不一致（禁止安装）" >&2
    echo "  要求 SHA: $SOURCE_SHA" >&2
    echo "  实际 HEAD: ${actual:-<非 git 目录>}" >&2
    exit 1
  fi
  echo "  源码 SHA 校验通过: $SOURCE_SHA"
else
  SOURCE_SHA="$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || echo "")"
  if [ -n "$SOURCE_SHA" ]; then
    echo "  源码 SHA（当前 HEAD）: $SOURCE_SHA"
  else
    echo "  WARN: 非 git 目录且未设置 FIREBOT_REQUIRE_SHA，将无法记录来源 SHA" >&2
  fi
fi

# 安装：只重建 firebot_control 包目录，不影响 src 下其它包。
mkdir -p "$ROS_SRC_DIR"
rm -rf "$PKG_DIR"
cp -r "$SCRIPT_DIR" "$PKG_DIR"
# install.sh 属于仓库交付工具，不放进 catkin 包目录。
rm -f "$PKG_DIR/install.sh"

if [ -n "$SOURCE_SHA" ]; then
  echo "$SOURCE_SHA" > "$PKG_DIR/APPROVED_RUNTIME.txt"
  echo "  已记录来源 SHA: $SOURCE_SHA"
fi

echo ""
echo "  完成。下一步："
echo "    cd $(dirname "$ROS_SRC_DIR") && catkin_make && source devel/setup.bash"
echo "  核对来源 SHA："
echo "    cat $PKG_DIR/APPROVED_RUNTIME.txt"
