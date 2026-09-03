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

# 安装：staging → 校验 → previous → atomic swap，只重建 firebot_control 包目录，
# 不影响 src 下其它包，也不在运行目录逐文件覆盖。
mkdir -p "$ROS_SRC_DIR"
WORKSPACE_DIR="$(cd "$(dirname "$ROS_SRC_DIR")" && pwd)"
STAGING_DIR="$WORKSPACE_DIR/.firebot_control.staging"
PREVIOUS_DIR="$WORKSPACE_DIR/.firebot_control.previous"

# staging 构建（放 workspace 根：catkin 只扫 src/，不会误扫 hidden staging/previous）
# 复制「目录内容」而非目录本身，保证 package.xml/CMakeLists.txt 位于 staging 根。
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"
cp -a "$SCRIPT_DIR"/. "$STAGING_DIR"/
# install.sh 属于仓库交付工具，不放进 catkin 包目录。
rm -f "$STAGING_DIR/install.sh"

# staging 完整性校验（关键文件缺失即中止，不做半安装）
[ -f "$STAGING_DIR/package.xml" ] || { echo "ERROR: staging 缺少 package.xml（中止安装）" >&2; exit 1; }
[ -f "$STAGING_DIR/CMakeLists.txt" ] || { echo "ERROR: staging 缺少 CMakeLists.txt（中止安装）" >&2; exit 1; }

if [ -n "$SOURCE_SHA" ]; then
  echo "$SOURCE_SHA" > "$STAGING_DIR/APPROVED_RUNTIME.txt"
  echo "  已记录来源 SHA: $SOURCE_SHA"
fi

# swap：旧版本保留为 previous，staging 原子替换为 current；失败自动回滚
rm -rf "$PREVIOUS_DIR"
if [ -d "$PKG_DIR" ]; then
  mv "$PKG_DIR" "$PREVIOUS_DIR"
fi
if ! mv "$STAGING_DIR" "$PKG_DIR"; then
  echo "ERROR: swap 失败，回滚到 previous" >&2
  [ -d "$PREVIOUS_DIR" ] && mv "$PREVIOUS_DIR" "$PKG_DIR"
  exit 1
fi

echo ""
echo "  完成。下一步："
echo "    cd $(dirname "$ROS_SRC_DIR") && catkin_make && source devel/setup.bash"
echo "  核对来源 SHA："
echo "    cat $PKG_DIR/APPROVED_RUNTIME.txt"
echo "  回滚：上一版本保留在 $PREVIOUS_DIR；"
echo "    mv $PKG_DIR /tmp/firebot_control.bad && mv $PREVIOUS_DIR $PKG_DIR && cd $(dirname "$ROS_SRC_DIR") && catkin_make"
